"""Map the host's bounded transform hook to plugin-owned reply shaping."""

from __future__ import annotations

from typing import Any

from ..core.contracts import ViewerEvent
from ..core.live_output_shape import shape_reply_text
from ..core.live_reply_contract import DISPATCH_REPLY_CHAR_LIMITS, is_live_reply_metadata, reply_limit_from_metadata

_CONTEXT_KEYS = frozenset({
    "live_reply_contract", "response_module_hint", "max_reply_chars", "reply_length_mode",
    "danmaku_profile", "danmaku_viewer_nickname", "danmaku_target_viewer_nickname",
    "viewer_claimed_support", "demo", "live_event_source", "delivery_epoch",
})


def _delivery_epoch(runtime: Any) -> str:
    token = getattr(runtime, "_response_transform_epoch", "")
    if not isinstance(token, str) or not token:
        return ""
    return ":".join(str(value) for value in (
        token, getattr(runtime, "_live_session_generation", 0),
        getattr(runtime, "_live_listener_operation_id", 0),
        getattr(getattr(runtime, "safety_guard", None), "_queue_generation", 0),
    ))


def response_transform_metadata(metadata: dict, *, runtime: Any = None) -> dict:
    if not is_live_reply_metadata(metadata) or metadata.get("response_module_hint") not in DISPATCH_REPLY_CHAR_LIMITS:
        return {}
    context = {key: value for key, value in metadata.items() if key in _CONTEXT_KEYS}
    # Transport-only snapshot, never copied into request/audit metadata. Always
    # replace producer-supplied epochs with the actual runtime/queue ownership.
    context["delivery_epoch"] = _delivery_epoch(runtime)
    return {"response_text_transform": {"version": 1, "context": context}}


def transform_generated_reply(runtime: Any, text: Any, context: Any) -> dict:
    if runtime is None or getattr(runtime, "_stopping", False) or not isinstance(context, dict):
        raise ValueError("output_policy.transform_failed")
    epoch = _delivery_epoch(runtime)
    if not epoch or context.get("delivery_epoch") != epoch:
        raise ValueError("output_policy.transform_failed")
    source = context.get("live_event_source")
    config = runtime.config
    if not isinstance(source, str) or config.dry_run or not runtime.permission_gate.allows_source(source)[0]:
        raise ValueError("output_policy.transform_failed")
    if context.get("demo") is True and not config.developer_tools_enabled:
        raise ValueError("output_policy.transform_failed")
    # Reuse final safety checks without consuming another output cooldown. The
    # original pipeline task is gone, so the explicit epoch above owns staleness.
    event = ViewerEvent(uid="", source=source)
    if not runtime.safety_guard.before_output(event, consume_cooldown=False).allowed:
        raise ValueError("output_policy.transform_failed")
    return shape_generated_reply(text, context)


def shape_generated_reply(text: Any, context: Any) -> dict:
    # This callback handles one freshly generated reply, never past history or
    # caller-provided already-shaped flags. It has no store or audit writes.
    if not isinstance(text, str) or not 1 <= len(text) <= 32768:
        raise ValueError("output_policy.transform_failed")
    if not isinstance(context, dict) or not set(context) <= _CONTEXT_KEYS:
        raise ValueError("output_policy.transform_failed")
    for key, value in context.items():
        if key == "demo":
            valid = type(value) is bool
        elif key == "max_reply_chars":
            valid = type(value) is int and 1 <= value <= 80
        else:
            valid = isinstance(value, str) and len(value) <= 512
        if not valid:
            raise ValueError("output_policy.transform_failed")
    if not is_live_reply_metadata(context) or context.get("response_module_hint") not in DISPATCH_REPLY_CHAR_LIMITS:
        raise ValueError("output_policy.transform_failed")
    limit = reply_limit_from_metadata(context)
    shaped, _diagnostics = shape_reply_text(text, context)
    shaped = shaped.strip()
    if not limit or not shaped.strip() or len(shaped) > limit:
        raise ValueError("output_policy.transform_failed")
    return {"text": shaped}
