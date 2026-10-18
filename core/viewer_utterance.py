"""Select authored live chat for local profiles and scoped viewer memory."""

from __future__ import annotations

from typing import Any

from .contracts_public import public_text


def viewer_utterance_text(event: Any) -> str:
    def field(name: str, default: Any = "") -> Any:
        return event.get(name, default) if isinstance(event, dict) else getattr(event, name, default)

    raw = field("raw", {})
    raw = raw if isinstance(raw, dict) else {}
    kind = str(raw.get("event_type") or field("event_type") or "").strip().lower()
    uid = str(field("uid") or "")
    if field("source") != "live_danmaku" or not uid or uid.startswith("__neko_"):
        return ""
    # SC bodies and provider-verified Twitch cheers are authored chat too;
    # generic gift/subscription/presence descriptions are not utterances.
    cheer = (
        kind == "gift" and raw.get("support_verified") is True
        and raw.get("support_evidence") == "twitch_eventsub_typed_event"
        and raw.get("provider_event_type") == "TWITCH_CHEER"
    )
    if kind not in {"", "danmaku", "danmu", "chat", "sc", "super_chat"} and not cheer:
        return ""
    text = field("danmaku_text")
    if raw.get("live_batch_welcome") == "new_viewer_burst":
        text = raw.get("original_danmaku_text")
    text = public_text(text, max_len=512)
    # The Bilibili parse-error fallback uses this label when no SC body exists.
    return "" if kind in {"sc", "super_chat"} and text.casefold() == "super chat" else text
