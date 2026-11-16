"""Bounded literal word-list settings; no regular expressions or model calls."""

from __future__ import annotations

import unicodedata

MAX_TERMS = 128
MAX_TERM_CHARS = 64
MAX_SETTING_CHARS = 8192


def match_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(char for char in value if unicodedata.category(char) != "Cf").split())


def normalize_terms(value: object, *, strict: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_SETTING_CHARS:
        if strict:
            raise ValueError("content_filter.invalid_terms")
        return ""
    terms: list[str] = []
    for line in value.splitlines():
        term = match_text(line.strip())
        if not term or term in terms:
            continue
        if len(term) > MAX_TERM_CHARS or len(terms) >= MAX_TERMS:
            if strict:
                raise ValueError("content_filter.invalid_terms")
            continue
        terms.append(term)
    return "\n".join(terms)
