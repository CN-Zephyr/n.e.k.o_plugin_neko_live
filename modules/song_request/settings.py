"""Bounded song-request settings shared by config loading and matching."""

from __future__ import annotations

import re
from typing import Any

from ..danmaku_filter.settings import normalize_match_text

DEFAULT_TRIGGER_TEXT = "点歌\n我想听\n来一首\n来首\n放一首\n放首\n求歌"
PERMISSIONS = ("anyone", "fans", "guard")
DEFAULT_COOLDOWN_SECONDS = 30
MAX_COOLDOWN_SECONDS = 3600
MAX_TRIGGERS = 32
MAX_TRIGGER_LENGTH = 32
MAX_SETTINGS_LENGTH = 2048


def normalize_triggers(value: Any) -> str:
    """Return newline-separated, de-duplicated triggers; fall back to the defaults."""
    if not isinstance(value, str) or len(value) > MAX_SETTINGS_LENGTH:
        return DEFAULT_TRIGGER_TEXT
    triggers: list[str] = []
    for item in re.split(r"\r\n?|\n", value):
        trigger = normalize_match_text(item)
        if not trigger or len(trigger) > MAX_TRIGGER_LENGTH or trigger in triggers:
            continue
        triggers.append(trigger)
        if len(triggers) >= MAX_TRIGGERS:
            break
    return "\n".join(triggers) or DEFAULT_TRIGGER_TEXT


def trigger_tuple(value: Any) -> tuple[str, ...]:
    """Triggers ordered longest first so "来一首" wins over "来首"."""
    triggers = normalize_triggers(value).split("\n")
    return tuple(sorted(triggers, key=len, reverse=True))


def normalize_permission(value: Any) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in PERMISSIONS else "anyone"


def normalize_cooldown(value: Any) -> int:
    if isinstance(value, bool):
        return DEFAULT_COOLDOWN_SECONDS
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SECONDS
    return max(0, min(MAX_COOLDOWN_SECONDS, seconds))
