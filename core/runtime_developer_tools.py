"""Developer sandbox actions for the runtime."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from .avatar_image import MAX_AVATAR_PREVIEW_BYTES, normalize_avatar_jpeg
from .contracts import InteractionResult, ViewerEvent, ViewerProfile
from .contracts_public import public_text
from .runtime_live_input import observe_live_danmaku, remember_live_danmaku_seen
from .runtime_live_session import bind_event_to_live_session


async def handle_sandbox_target(runtime: Any, **kwargs: Any) -> InteractionResult:
    require_developer_mode(runtime)
    event = runtime.developer_sandbox.parse_target(**kwargs)
    return await runtime.pipeline.handle_event(event)


async def lookup_bili_user(
    runtime: Any, *, include_avatar_preview: bool = False, **kwargs: Any,
) -> dict[str, Any]:
    require_developer_mode(runtime)
    event = runtime.developer_sandbox.parse_target(**kwargs, use_presets=False)
    if not event.uid:
        raise ValueError("uid or Bilibili space URL is required")
    identity = await runtime.bili_identity.resolve(event)
    require_developer_mode(runtime)
    preview = ""
    if include_avatar_preview is True and runtime.config.avatar_analysis_enabled and not identity.is_default_avatar:
        thumbnail = await asyncio.to_thread(
            normalize_avatar_jpeg,
            identity.avatar_bytes or b"",
            edges=(144, 96, 72),
            budget_bytes=MAX_AVATAR_PREVIEW_BYTES,
        )
        require_developer_mode(runtime)
        if thumbnail and runtime.config.avatar_analysis_enabled:
            preview = "data:image/jpeg;base64," + base64.b64encode(thumbnail).decode("ascii")
    profile = ViewerProfile(uid=identity.uid, nickname=identity.nickname, avatar_url=identity.avatar_url)
    identity_payload = identity.to_public_dict()
    identity_payload["avatar_preview_url"] = ""
    identity_payload["avatar_preview_data_url"] = ""
    runtime.audit.record(
        "developer_lookup",
        "bili user looked up",
        detail={"uid": identity.uid, "fetched": identity.fetched},
    )
    payload = {
        "event": event.to_dict(),
        "identity": identity_payload,
        "profile": profile.to_dict(),
    }
    # Explicit developer action response only; never add image data to public
    # identities, dashboard snapshots, recent results, audit or persistent files.
    if include_avatar_preview is True:
        payload["avatar_preview_data_url"] = preview
    return payload


def clear_sandbox_data(runtime: Any) -> dict[str, Any]:
    require_developer_mode(runtime)
    cleared_records = len(runtime.recent_sandbox_results)
    runtime.recent_sandbox_results.clear()
    cleared_preview_files = 0
    preview_dir = runtime.plugin_dir / "static" / "avatar-preview"
    if preview_dir.is_dir():
        for path in preview_dir.iterdir():
            if not path.is_file():
                continue
            try:
                path.unlink()
                cleared_preview_files += 1
            except OSError:
                runtime.audit.record("sandbox_preview_clear_failed", path.name, level="warning")
    runtime.audit.record(
        "sandbox_clear",
        "sandbox runtime data cleared",
        detail={"records": cleared_records, "preview_files": cleared_preview_files},
    )
    return {"records": cleared_records, "preview_files": cleared_preview_files}


def require_developer_mode(runtime: Any) -> None:
    if not runtime.config.developer_tools_enabled:
        raise PermissionError("developer mode is disabled")


async def handle_manual_event(runtime: Any, **kwargs: Any) -> InteractionResult:
    require_developer_mode(runtime)
    uid = kwargs.get("uid")
    if type(uid) is int and uid > 0:
        uid = str(uid)
    event = ViewerEvent(
        uid=public_text(uid, max_len=128),
        nickname=public_text(kwargs.get("nickname"), max_len=80),
        avatar_url=public_text(kwargs.get("avatar_url"), max_len=512),
        danmaku_text=public_text(kwargs.get("danmaku_text"), max_len=512),
        target_lanlan=public_text(kwargs.get("target_lanlan") or kwargs.get("lanlan_name"), max_len=80),
        source="manual_live_simulation",
        live_mode=runtime.config.live_mode,
        raw={"event_type": "danmaku"},
    )
    bind_event_to_live_session(runtime, event)
    observed = observe_live_danmaku(runtime, event)
    if observed and event.uid and runtime.config.live_enabled and runtime.safety_guard.connected:
        remember_live_danmaku_seen(runtime, event)
    return await runtime.pipeline.handle_event(event)
