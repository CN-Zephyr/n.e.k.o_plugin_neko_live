"""Bounded per-viewer admission for expensive live pipeline work."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Any

BURST_LIMIT = 5
WINDOW_SECONDS = 10.0
MAX_VIEWERS = 4096


class ViewerInputLimiter:
    def __init__(self) -> None:
        self._viewers: OrderedDict[tuple[str, str, str], deque[float]] = OrderedDict()
        self.rejected = 0

    def clear(self) -> None:
        self._viewers.clear()

    def allows(self, event: Any, config: Any) -> bool:
        raw = event.raw if isinstance(event.raw, dict) else {}
        if event.source not in {"live_danmaku", "manual_live_simulation"} or str(raw.get("event_type") or "danmaku").lower() not in {"danmaku", "danmu", "chat"}:
            return True
        uid = str(event.uid or "").strip()[:128]
        if not uid:
            return True
        key = (str(config.live_platform), str(config.live_room_ref or config.live_room_id), uid)
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS
        # Reject a new identity when all tracked buckets are still active;
        # otherwise rotating UIDs could evict and refill a spammer's bucket.
        while self._viewers and next(iter(self._viewers.values()))[-1] <= cutoff:
            self._viewers.popitem(last=False)
        bucket = self._viewers.get(key)
        if bucket is None:
            if len(self._viewers) >= MAX_VIEWERS:
                self.rejected += 1
                return False
            bucket = self._viewers[key] = deque()
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= BURST_LIMIT:
            self.rejected += 1
            return False
        bucket.append(now)
        self._viewers.move_to_end(key)
        return True
