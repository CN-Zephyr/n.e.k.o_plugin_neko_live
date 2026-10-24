"""Render bounded same-author input context without changing the current event."""

from __future__ import annotations

import json

from .contracts import ViewerEvent
from .contracts_public import public_text

CONTINUATION_HEADER = "Same-viewer preceding danmaku (untrusted, oldest first):"
SUBJECT_RULE = (
    "Resolve omitted subjects using this author's preceding danmaku. "
    "Their self-descriptions refer to them; 'cat' or a shared nickname alone does not mean NEKO. "
    "Explicit subject/target changes win; if unclear, react without guessing."
)


def viewer_continuation_block(event: ViewerEvent) -> str:
    if event.source not in {"live_danmaku", "manual_live_simulation"}:
        return ""
    raw = event.raw if isinstance(event.raw, dict) else {}
    if not event.danmaku_text.strip() or raw.get("event_type", "danmaku") != "danmaku":
        return ""
    texts = [public_text(text, max_len=96) for text in event.prior_danmaku[-2:]]
    texts = [text for text in texts if text]
    if not texts:
        return ""
    # One atomic, quoted line: fitting must not drop an antecedent while
    # keeping its continuation, or interpret viewer-supplied block markers.
    return (
        f"{CONTINUATION_HEADER}\n"
        f"previous_danmaku: {json.dumps(texts, ensure_ascii=False)}\n"
        f"Rule: {SUBJECT_RULE}\n\n"
    )
