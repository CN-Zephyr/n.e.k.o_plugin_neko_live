"""Shared safety and similarity rules for plugin-owned live material."""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

from .active_topic_safety import is_clean_live_material

_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
_SIMILARITY_THRESHOLD = 0.78
_MIN_NORMALIZED_CHARS = 6

__all__ = ["is_clean_live_material", "is_similar_live_material_title"]


def is_similar_live_material_title(title: str, recent_titles: Iterable[str]) -> bool:
    normalized = _normalize_title(title)
    if len(normalized) < _MIN_NORMALIZED_CHARS:
        return False
    for previous in recent_titles:
        previous_normalized = _normalize_title(previous)
        if len(previous_normalized) < _MIN_NORMALIZED_CHARS:
            continue
        if normalized == previous_normalized:
            return True
        shorter, longer = sorted((normalized, previous_normalized), key=len)
        if shorter in longer:
            return True
        if SequenceMatcher(None, normalized, previous_normalized).ratio() >= _SIMILARITY_THRESHOLD:
            return True
    return False


def _normalize_title(text: str) -> str:
    return _NORMALIZE_RE.sub("", str(text or "").casefold())

