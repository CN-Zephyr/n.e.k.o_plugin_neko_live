"""Main post-safety flow for routed pipeline events."""

from __future__ import annotations

import asyncio
from typing import Any

from .contracts import (
    InteractionResult,
    PipelineStep,
    ViewerEvent,
    ViewerIdentity,
    ViewerProfile,
)
from .pipeline_dispatch import dispatch_routed_request
from .pipeline_requests import build_request_for_route, build_song_request_reply
from .pipeline_results import (
    fail_pipeline,
    skip_already_roasted,
    skip_module_disabled,
    skip_song_request_cooldown,
    skip_stale_live_event,
)
from .pipeline_routing import support_event_type
from .pipeline_viewers import resolve_viewer_context
from .runtime_live_session import is_current_live_session_event
from .runtime_timeline import record_timeline


async def run_event_flow(
    pipeline: Any,
    event: ViewerEvent,
    steps: list[PipelineStep],
) -> InteractionResult:
    ctx = pipeline.ctx
    session = pipeline.session
    try:
        is_transient_event = pipeline._is_transient_event(event)
        viewer = await resolve_viewer_context(
            ctx,
            event,
            steps,
            is_transient_event=is_transient_event,
            fetch_avatar_image=not bool(support_event_type(event)),
        )
        identity = viewer.identity
        profile = viewer.profile
        if not viewer.session_current:
            return skip_stale_live_event(ctx, event, identity, profile, steps)

        # 点歌拦截：放在 stale 检查之后、普通路由之前；不受 already_roasted 影响，也不标记锐评。
        song_request_result = await _maybe_handle_song_request(
            pipeline, event, identity, profile, steps
        )
        if song_request_result is not None:
            return song_request_result

        uid_lock: asyncio.Lock | None = None
        needs_session_gate = pipeline._is_live_danmaku_with_text(event)
        if session.needs_uid_lock(
            ctx.config,
            is_live_danmaku_with_text=needs_session_gate,
            is_transient_event=is_transient_event,
        ):
            uid_lock = await session.acquire_uid_lock(identity.uid)
        try:
            if not is_current_live_session_event(ctx, event):
                return skip_stale_live_event(ctx, event, identity, profile, steps)
            already_roasted = False
            if uid_lock is not None:
                already_roasted = await session.already_roasted(
                    ctx,
                    event,
                    identity.uid,
                )
            has_uid_lock = uid_lock is not None
            route = pipeline._route_for_event(
                event,
                is_transient_event=is_transient_event,
                has_uid_lock=has_uid_lock,
                already_roasted=already_roasted,
            )
            module_flag = {
                "avatar_roast": "avatar_roast_enabled",
                "danmaku_response": "danmaku_response_enabled",
                "live_support_events": "live_support_events_enabled",
                "warmup_hosting": "warmup_hosting_enabled",
                "idle_hosting": "idle_hosting_enabled",
                "active_engagement": "active_engagement_enabled",
            }.get(route.response_module_id)
            presence_flag = {
                "like": "like_greet_enabled", "follow": "follow_greet_enabled",
            }.get(support_event_type(event))
            if presence_flag and not bool(getattr(ctx.config, presence_flag, False)):
                module_id = presence_flag.removesuffix("_enabled")
                record_timeline(
                    ctx, event, stage="module_gate", status="skipped",
                    reason=f"{module_id}.disabled", route=route.response_module_id,
                )
                return skip_module_disabled(ctx, event, identity, profile, steps, module_id)
            if route.response_module_id == "live_presence" or (
                module_flag and not bool(getattr(ctx.config, module_flag, True))
            ):
                record_timeline(
                    ctx,
                    event,
                    stage="module_gate",
                    status="skipped",
                    reason=f"{route.response_module_id}.disabled",
                    route=route.response_module_id,
                )
                return skip_module_disabled(
                    ctx,
                    event,
                    identity,
                    profile,
                    steps,
                    route.response_module_id,
                )
            record_timeline(
                ctx,
                event,
                stage="pipeline.route",
                status="ok",
                reason=route.viewer_gate_reason,
                route=route.response_module_id,
            )
            # already_roasted 只表示「这个观众别再被锐评第二次」，只作用于锐评类回复。
            # 礼物 / SC / 舰长走 live_support_events，是致谢；发过弹幕的观众送礼仍要道谢。
            _already_roasted_exempt = route.response_module_id == "live_support_events" or (
                route.response_module_id == "danmaku_response"
                and hasattr(ctx, "danmaku_response")
            )
            if (
                uid_lock is not None
                and already_roasted
                and route.viewer_gate_reason != "explicit_self_avatar_roast"
                and not _already_roasted_exempt
            ):
                record_timeline(
                    ctx,
                    event,
                    stage="viewer_gate",
                    status="skipped",
                    reason="already_roasted",
                    route=route.response_module_id,
                )
                return skip_already_roasted(ctx, event, identity, profile, steps)

            if route.viewer_gate_reason:
                steps.append(
                    PipelineStep("viewer_gate", "ok", route.viewer_gate_reason)
                )
            else:
                steps.append(PipelineStep("viewer_gate", "ok"))
            request = build_request_for_route(ctx, route, event, identity, profile)
            should_mark_roasted = route.should_mark_roasted
            response_module_id = route.response_module_id
            steps.append(PipelineStep(response_module_id, "ok"))
            record_timeline(
                ctx,
                event,
                stage="request.build",
                status="ok",
                reason=request.reason,
                route=response_module_id,
            )
            return await dispatch_routed_request(
                ctx,
                session,
                event=event,
                identity=identity,
                profile=profile,
                request=request,
                steps=steps,
                response_module_id=response_module_id,
                should_mark_roasted=should_mark_roasted,
                mark_avatar_roast_sent=pipeline._record_avatar_roast_sent,
            )
        finally:
            if uid_lock is not None:
                session.release_uid_lock(identity.uid, uid_lock)
    except Exception as exc:
        return fail_pipeline(ctx, event, steps, exc)
    finally:
        ctx.safety_guard.after_event()


SONG_REQUEST_MODULE_ID = "song_request"
SONG_REQUEST_COOLDOWN_REASON = "song_request.cooldown"


async def _maybe_handle_song_request(
    pipeline: Any,
    event: ViewerEvent,
    identity: ViewerIdentity,
    profile: ViewerProfile,
    steps: list[PipelineStep],
) -> InteractionResult | None:
    """Handle a live song request danmaku, or return None to keep normal routing."""

    if not pipeline._is_live_danmaku_with_text(event):
        return None
    ctx = pipeline.ctx
    module = getattr(ctx, SONG_REQUEST_MODULE_ID, None)
    match = getattr(module, "match", None)
    handle = getattr(module, "handle", None)
    # 模块缺失或仍是 ReservedModule 占位时视为功能不可用，走原有路由。
    if not callable(match) or not callable(handle):
        return None
    query = match(event.danmaku_text or "", event_type="danmaku", config=ctx.config)
    if query is None:
        return None
    outcome = await handle(ctx, event, query)
    if str(getattr(outcome, "status", "") or "") == "cooldown":
        # 冷却中直接静默，避免刷屏。
        record_timeline(
            ctx,
            event,
            stage=SONG_REQUEST_MODULE_ID,
            status="skipped",
            reason=SONG_REQUEST_COOLDOWN_REASON,
            route=SONG_REQUEST_MODULE_ID,
        )
        return skip_song_request_cooldown(
            ctx, event, identity, profile, steps, SONG_REQUEST_COOLDOWN_REASON
        )
    request = build_song_request_reply(ctx, outcome, event, identity, profile)
    steps.append(PipelineStep(SONG_REQUEST_MODULE_ID, "ok", request.metadata.get("song_request_status", "")))
    record_timeline(
        ctx,
        event,
        stage="request.build",
        status="ok",
        reason=request.reason,
        route=SONG_REQUEST_MODULE_ID,
    )
    return await dispatch_routed_request(
        ctx,
        pipeline.session,
        event=event,
        identity=identity,
        profile=profile,
        request=request,
        steps=steps,
        response_module_id=SONG_REQUEST_MODULE_ID,
        should_mark_roasted=False,
        mark_avatar_roast_sent=lambda: None,
    )
