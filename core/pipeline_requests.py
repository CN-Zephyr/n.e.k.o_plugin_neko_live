"""Request construction for routed pipeline events."""

from __future__ import annotations

from typing import Any

from ..modules._prompt_context import (
    recent_context_block,
    viewer_preference_context_block,
    viewer_session_context_block,
)
from ..modules._prompt_song_request import build_song_request_prompt
from .contracts import InteractionRequest, ViewerEvent, ViewerIdentity, ViewerProfile
from .contracts_public import public_text
from .live_host_theme import live_host_theme_block
from .pipeline_routing import PipelineRoute


def build_request_for_route(
    ctx: Any,
    route: PipelineRoute,
    event: ViewerEvent,
    identity: ViewerIdentity,
    profile: ViewerProfile,
) -> InteractionRequest:
    if route.response_module_id == "warmup_hosting":
        request = ctx.warmup_hosting.build_request(event, identity, profile)
    elif route.response_module_id == "active_engagement":
        request = ctx.active_engagement.build_request(event, identity, profile)
    elif route.response_module_id == "idle_hosting":
        # Idle hosting reuses the avatar roast request shape in this slice.
        request = ctx.avatar_roast.build_request(event, identity, profile)
    elif route.response_module_id == "live_support_events":
        request = ctx.live_support_events.build_request(event, identity, profile)
    elif route.response_module_id == "danmaku_response":
        request = ctx.danmaku_response.build_request(event, identity, profile)
    else:
        request = ctx.avatar_roast.build_request(event, identity, profile)
    # The pipeline route owns module identity. Visual availability is an input
    # capability and must never silently reclassify avatar_roast as a normal
    # danmaku response at the Dispatcher contract boundary.
    request.metadata["response_module_hint"] = route.response_module_id
    return request


def build_song_request_reply(
    ctx: Any,
    outcome: Any,
    event: ViewerEvent,
    identity: ViewerIdentity,
    profile: ViewerProfile,
) -> InteractionRequest:
    """Build the LLM reply request for a handled song request outcome."""
    nickname = public_text(
        identity.nickname or identity.uid or "this viewer",
        max_len=80,
    ) or "this viewer"
    public_uid = public_text(identity.uid, max_len=80) or "unknown"
    config = getattr(ctx, "config", None)
    strength = getattr(config, "roast_strength", "normal") if config else "normal"
    permission_level = getattr(config, "song_request_permission", "anyone") if config else "anyone"

    prompt_text = build_song_request_prompt(
        outcome,
        nickname,
        public_uid,
        event.danmaku_text or "",
        live_host_theme_block(ctx, kind="reply"),
        recent_context_block(ctx, follow_up=True),
        viewer_session_context_block(ctx, identity.uid, follow_up=True),
        viewer_preference_context_block(ctx, profile),
        permission_level,
    )

    metadata = {
        "response_module_hint": "song_request",
        "song_request_status": str(getattr(outcome, "status", "error") or "error"),
    }
    title = getattr(outcome, "title", None)
    if title:
        metadata["queued_song_title"] = public_text(title, max_len=120)
    artist = getattr(outcome, "artist", None)
    if artist:
        metadata["queued_song_artist"] = public_text(artist, max_len=120)

    return InteractionRequest(
        event=event,
        identity=identity,
        profile=profile,
        prompt_text=prompt_text,
        live_mode=event.live_mode,
        strength=strength,
        dry_run=bool(getattr(config, "dry_run", False)) if config else False,
        allow_avatar_image=False,
        metadata=metadata,
    )
