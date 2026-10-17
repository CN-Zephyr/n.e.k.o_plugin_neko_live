"""Default opt-out policy for ordinary danmaku, shared by every ingress."""

from __future__ import annotations

from typing import Any

from ...core.contracts_events import ViewerEvent
from .._base import BaseModule
from .settings import DEFAULT_PREFIX_TEXT, normalize_match_text, normalize_prefixes

IGNORED_PREFIX_REASON = "input.ignored_prefix"
_CHAT_TYPES = {"", "danmaku", "danmu", "chat"}
_CHAT_SOURCES = {"live_danmaku", "manual_live_simulation", "developer_sandbox"}


def _prefix_reason(text: str, *, event_type: str = "danmaku", prefixes: tuple[str, ...] = ("!", "！")) -> str:
    if str(event_type or "").strip().lower() not in _CHAT_TYPES:
        return ""
    return IGNORED_PREFIX_REASON if normalize_match_text(text or "").startswith(prefixes) else ""


class DanmakuFilterModule(BaseModule):
    id = "danmaku_filter"
    title = "Danmaku filter"
    domain = "interaction"

    def __init__(self) -> None:
        super().__init__()
        self._prefix_source = DEFAULT_PREFIX_TEXT
        self._prefixes = tuple(DEFAULT_PREFIX_TEXT.splitlines())

    def reason(self, text: str, *, event_type: str = "danmaku", config: Any = None) -> str:
        config = config if config is not None else getattr(self.ctx, "config", None)
        if not self.enabled or getattr(config, "danmaku_filter_enabled", True) is not True:
            return ""
        source = getattr(config, "danmaku_filter_prefixes", DEFAULT_PREFIX_TEXT)
        if source != self._prefix_source:
            self._prefix_source = source
            self._prefixes = tuple(normalize_prefixes(source).splitlines())
        return _prefix_reason(text, event_type=event_type, prefixes=self._prefixes)

    def config_schema(self) -> list[dict[str, Any]]:
        return [
            {"name": "danmaku_filter_enabled", "type": "boolean", "label": "panel.interaction.module.danmakuFilter.title", "default": True},
            {"name": "danmaku_filter_prefixes", "type": "text", "label": "panel.interaction.module.danmakuFilter.prefixes", "default": DEFAULT_PREFIX_TEXT},
        ]


def skip_reason(ctx: Any, text: str, *, event_type: str = "danmaku") -> str:
    module = getattr(ctx, "danmaku_filter", None)
    if module is None:
        # Small standalone pipeline/consumer contexts use the default rule too.
        return DanmakuFilterModule().reason(text, event_type=event_type, config=getattr(ctx, "config", None))
    reason = getattr(module, "reason", None)
    return reason(text, event_type=event_type, config=getattr(ctx, "config", None)) if callable(reason) else ""


def viewer_event_skip_reason(ctx: Any, event: ViewerEvent) -> str:
    if event.source not in _CHAT_SOURCES:
        return ""
    raw = event.raw if isinstance(event.raw, dict) else {}
    return skip_reason(ctx, event.danmaku_text, event_type=raw.get("event_type") or "")
