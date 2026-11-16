"""Streamer-owned text exclusions before input collection and host delivery."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .settings import match_text, normalize_terms

BLOCKED_REASON = "content.blocked_term"


@lru_cache(maxsize=1)
def _terms(source: str) -> tuple[str, ...]:
    return tuple(normalize_terms(source).splitlines())


def response_policy_metadata(config: Any) -> dict[str, Any]:
    """Snapshot the bounded host contract for respond messages only."""
    source = getattr(config, "content_filter_terms", "")
    terms = _terms(source) if isinstance(source, str) else ()
    return {"response_text_policy": {"version": 1, "blocked_terms": list(terms)}} if terms else {}


def skip_reason(config: Any, *values: object) -> str:
    source = getattr(config, "content_filter_terms", "")
    terms = _terms(source) if isinstance(source, str) else ()
    if not terms:
        return ""
    for value in values:
        if not isinstance(value, str):
            continue
        # Do not scan arbitrarily large provider input just to enforce a limit.
        if len(value) > 32768:
            return "content.input_too_large"
        text = match_text(value)
        if any(term in text for term in terms):
            return BLOCKED_REASON
    return ""
