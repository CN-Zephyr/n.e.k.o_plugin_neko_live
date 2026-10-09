"""Prompt context blocks shared by live interaction modules."""

from __future__ import annotations

from typing import Any

from ..core.meme_knowledge import render_meme_knowledge_block, retrieve_meme_knowledge
from ..core.viewer_preferences import viewer_preference_prompt_block
from ._prompt_context_compaction import compact_context_line

RECENT_CONTEXT_DEFAULT_LIMIT = 12
RECENT_CONTEXT_LINE_LIMIT = 56
VIEWER_CONTEXT_DEFAULT_LIMIT = 6
VIEWER_CONTEXT_LINE_LIMIT = 44
ROOM_CONTEXT_DEFAULT_LIMIT = 6
ROOM_CONTEXT_LINE_LIMIT = 96


def recent_context_block(
    ctx: Any, *, limit: int = RECENT_CONTEXT_DEFAULT_LIMIT, follow_up: bool = False
) -> str:
    provider = getattr(ctx, "recent_interaction_context", None)
    if not callable(provider):
        return ""
    try:
        raw_lines = provider(limit=limit)
    except TypeError:
        raw_lines = provider()
    except Exception:
        return ""
    if not isinstance(raw_lines, list):
        return ""
    lines = [compact_context_line(line, limit=RECENT_CONTEXT_LINE_LIMIT) for line in raw_lines]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return (
        "Recent spent live material:\n"
        + "\n".join(f"- {line}" for line in lines[:limit])
        + "\n\n"
        + "Rule: these lines are recent room talk. Do not copy NEKO's previous sentence, joke, or host beat.\n"
        + (
            "The current input always wins. If it follows this talk, answer the follow-up; otherwise keep the reply short.\n"
            if follow_up
            else "Do not continue an old topic from these lines. Answer only the current task.\n"
        )
    )


def audience_digest_block(ctx: Any) -> str:
    """Optional one-line room pulse. Default off, so ordinary replies stay unchanged."""

    config = getattr(ctx, "config", None)
    if getattr(config, "audience_digest_enabled", False) is not True:
        return ""
    session = getattr(ctx, "live_audience_session", None)
    snapshot = getattr(session, "snapshot", None)
    if not callable(snapshot):
        return ""
    try:
        data = snapshot()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    entries = int(data.get("entry_count") or 0)
    follows = int(data.get("follow_count") or 0)
    likes = int(data.get("like_count") or 0)
    if entries == 0 and follows == 0 and likes == 0:
        return ""
    like_total = data.get("like_total")
    total = f", like_total={int(like_total)}" if isinstance(like_total, int) else ""
    return (
        "Room pulse, background only:\n"
        f"- entries={entries}, follows={follows}, likes={likes}{total}\n"
        "Do not read these counts aloud unless the current task is idle hosting.\n\n"
    )


def _viewer_context_limit(ctx: Any, limit: int | None) -> int:
    chosen = limit
    if chosen is None:
        configured = getattr(
            getattr(ctx, "config", None),
            "viewer_context_limit",
            VIEWER_CONTEXT_DEFAULT_LIMIT,
        )
        try:
            chosen = int(configured)
        except (TypeError, ValueError):
            chosen = VIEWER_CONTEXT_DEFAULT_LIMIT
    return max(1, min(12, chosen))


def viewer_session_context_block(
    ctx: Any, uid: str, *, limit: int | None = None, follow_up: bool = False
) -> str:
    provider = getattr(ctx, "viewer_session_context", None)
    if not callable(provider):
        return ""
    limit = _viewer_context_limit(ctx, limit)
    try:
        raw_lines = provider(uid, limit=limit)
    except TypeError:
        raw_lines = provider(uid)
    except Exception:
        return ""
    if not isinstance(raw_lines, list):
        return ""
    lines = [compact_context_line(line, limit=VIEWER_CONTEXT_LINE_LIMIT) for line in raw_lines]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return (
        "Same-viewer recent talk:\n"
        + "\n".join(f"- {line}" for line in lines[:limit])
        + "\n\n"
        + "Rule: these lines show who said what. Do not repeat NEKO's previous sentence.\n"
        + (
            "If the current danmaku follows this thread, answer the follow-up. One remembered fact may be mentioned once, in ordinary words.\n"
            if follow_up
            else "Do not continue this viewer's previous topic.\n"
        )
    )


def viewer_preference_context_block(ctx: Any, profile: Any) -> str:
    """Render durable personalization only when the streamer enabled it."""

    config = getattr(ctx, "config", None)
    if getattr(config, "viewer_memory_enabled", True) is False:
        return ""
    return viewer_preference_prompt_block(profile)


def room_danmaku_context_block(
    ctx: Any,
    event: Any,
    *,
    limit: int = ROOM_CONTEXT_DEFAULT_LIMIT,
) -> str:
    provider = getattr(ctx, "recent_room_danmaku_context", None)
    if not callable(provider):
        return ""
    try:
        raw_lines = provider(event, limit=limit)
    except TypeError:
        raw_lines = provider(event)
    except Exception:
        return ""
    if not isinstance(raw_lines, list):
        return ""
    lines = [compact_context_line(line, limit=ROOM_CONTEXT_LINE_LIMIT) for line in raw_lines]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return (
        "Recent room theme context:\n"
        + "\n".join(f"- {line}" for line in lines[:limit])
        + "\n\n"
        + "Rule: answer the current danmaku first; use a shared theme only as one compact bridge. Silently ignore low-value repeats and never announce stored context or counts.\n"
    )


def live_events_context_block(ctx: Any, event: Any) -> str:
    live_events = getattr(ctx, "live_events", None) if ctx is not None else None
    provider = getattr(live_events, "prompt_block_for_event", None)
    if not callable(provider):
        return ""
    try:
        return str(provider(event) or "")
    except Exception:
        return ""


def meme_knowledge_context_block(*parts: str, limit: int = 2) -> str:
    try:
        entries = retrieve_meme_knowledge(*parts, limit=limit)
    except Exception:
        return ""
    return render_meme_knowledge_block(entries)
