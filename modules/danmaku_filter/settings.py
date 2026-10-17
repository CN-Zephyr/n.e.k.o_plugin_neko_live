"""Bounded literal-prefix settings shared by config loading and filtering."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_PREFIX_TEXT = "!\n！"
MAX_PREFIXES = 32
MAX_PREFIX_LENGTH = 64
MAX_SETTINGS_LENGTH = 8192


def normalize_match_text(value: str) -> str:
    """Match the whitespace folding used by live text projections."""
    return " ".join(value.split())


def normalize_prefixes(value: Any, *, strict: bool = False) -> str:
    if isinstance(value, str) and len(value) <= MAX_SETTINGS_LENGTH:
        items = re.split(r"\r\n?|\n", value)
    elif isinstance(value, (list, tuple)) and len(value) <= MAX_SETTINGS_LENGTH:
        items = value
    else:
        if strict:
            raise ValueError("danmaku_filter.invalid_prefix_list")
        return DEFAULT_PREFIX_TEXT
    prefixes: list[str] = []
    for item in items:
        if not isinstance(item, str):
            if strict:
                raise ValueError("danmaku_filter.invalid_prefix_list")
            continue
        prefix = item.strip()
        if not prefix:
            continue
        if len(prefix) > MAX_PREFIX_LENGTH or "\n" in prefix or "\r" in prefix:
            if strict:
                raise ValueError("danmaku_filter.prefix_too_long")
            continue
        prefix = normalize_match_text(prefix)
        if prefix in prefixes:
            continue
        if len(prefixes) >= MAX_PREFIXES:
            if strict:
                raise ValueError("danmaku_filter.too_many_prefixes")
            break
        prefixes.append(prefix)
    return "\n".join(prefixes)
