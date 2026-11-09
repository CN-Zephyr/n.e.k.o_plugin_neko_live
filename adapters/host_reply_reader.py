"""Bounded, in-memory observations of replies owned by this live session."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.live_output_memory import compact_recent_reply_values

QUERY_INTERVAL_SECONDS = 2.0
QUERY_TIMEOUT_SECONDS = 0.2
QUERY_MAX_COUNT = 32
MAX_REQUEST_REFS = 128
MAX_EVENT_REFS = 64
MAX_REPLY_VALUES = 6
MAX_REPLY_CHARS = 24
MAX_PROVENANCE_SOURCES = 16
MAX_ID_CHARS = 128
CACHE_TTL_SECONDS = 120.0
REQUEST_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class ReplyScope:
    target: str
    session_generation: int
    operation_id: int


def _field(value: object, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _opaque_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_CHARS and value.strip() == value


def _timestamp(record: object, metadata: Mapping[str, Any]) -> float:
    for value in (_field(record, "timestamp"), metadata.get("ts")):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                number = float(value)
            except (OverflowError, ValueError):
                continue
            if math.isfinite(number) and number >= 0:
                return number
    return 0.0


class HostReplyReader:
    """Query only on demand; never infer ownership from role or timing alone.

    Request references are registered only after a successful host submission.
    The caller owns live-session validation before calling ``submitted`` and
    after awaiting ``prepare``. No observation is written to plugin stores.
    """

    def __init__(self, plugin: Any, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._plugin = plugin
        self._monotonic = monotonic
        self._scope: ReplyScope | None = None
        self._epoch = 0
        self._closed = False
        self._pending: OrderedDict[str, float] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._replies: list[tuple[float, int, float, str]] = []
        self._sequence = 0
        self._last_attempt_at: float | None = None
        self._inflight: asyncio.Task[None] | None = None

    def reset(self) -> None:
        """Invalidate observations and in-flight work before a scope changes."""
        self._epoch += 1
        self._scope = None
        self._pending.clear()
        self._seen.clear()
        self._replies.clear()
        self._sequence = 0
        task, self._inflight = self._inflight, None
        if task is not None and not task.done():
            task.cancel()
        # Keep the query budget across rapid disconnects / scope switches.

    def close(self) -> None:
        self._closed = True
        self.reset()

    def reopen(self) -> None:
        self.reset()
        self._closed = False

    def _bind(self, scope: ReplyScope) -> bool:
        if self._closed or not isinstance(scope, ReplyScope) or not scope.target:
            return False
        if self._scope != scope:
            self.reset()
            self._scope = scope
        return True

    def submitted(self, scope: ReplyScope, request_id: str) -> None:
        if not _opaque_id(request_id) or not self._bind(scope):
            return
        now = self._monotonic()
        self._expire(now)
        self._pending[request_id] = now
        self._pending.move_to_end(request_id)
        while len(self._pending) > MAX_REQUEST_REFS:
            self._pending.popitem(last=False)

    def _values(self) -> list[str]:
        return [text for _stamp, _sequence, _observed_at, text in self._replies]

    def _expire(self, now: float) -> None:
        while self._pending:
            request_id, submitted_at = next(iter(self._pending.items()))
            if now - submitted_at < REQUEST_TTL_SECONDS:
                break
            self._pending.pop(request_id)
        self._replies = [
            entry for entry in self._replies if now - entry[2] < CACHE_TTL_SECONDS
        ]

    def _current(self, scope: ReplyScope, epoch: int) -> bool:
        return not self._closed and self._scope == scope and self._epoch == epoch

    async def prepare(self, scope: ReplyScope) -> list[str]:
        if not self._bind(scope):
            return []
        now = self._monotonic()
        self._expire(now)
        if not self._pending:
            return self._values()
        epoch = self._epoch
        task = self._inflight
        if task is None or task.done():
            if self._last_attempt_at is not None and now - self._last_attempt_at < QUERY_INTERVAL_SECONDS:
                return self._values()
            self._last_attempt_at = now
            task = asyncio.create_task(self._query(scope, epoch, frozenset(self._pending)))
            self._inflight = task
            task.add_done_callback(self._query_done)
        try:
            # A cleared queue can cancel one waiter without cancelling another
            # valid caller sharing the same bounded RPC.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            # Reset cancels the shared RPC, not the live-hosting caller. Its
            # invalidated preparation is an empty observation, so a long-lived
            # caller can recheck the session and continue its next iteration.
            if not self._current(scope, epoch):
                return []
            raise
        finally:
            if task.done() and self._inflight is task:
                self._inflight = None
        if not self._current(scope, epoch):
            return []
        self._expire(self._monotonic())
        return self._values()

    def _query_done(self, task: asyncio.Task[None]) -> None:
        if self._inflight is task:
            self._inflight = None
        if not task.cancelled():
            task.exception()

    async def _query(self, scope: ReplyScope, epoch: int, pending: frozenset[str]) -> None:
        try:
            ctx = getattr(self._plugin, "ctx", None)
            conversations = getattr(getattr(ctx, "bus", None), "conversations", None)
            getter = getattr(conversations, "get", None)
            if not callable(getter):
                return
            result = getter(max_count=QUERY_MAX_COUNT, timeout=QUERY_TIMEOUT_SECONDS)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=QUERY_TIMEOUT_SECONDS)
            if not self._current(scope, epoch):
                return
            self._expire(self._monotonic())
            pending = pending.intersection(self._pending)
            if not pending:
                return
            rows = result if isinstance(result, (list, tuple)) else _field(result, "items")
            if not isinstance(rows, (list, tuple)):
                return
            accepted: list[tuple[float, str, str]] = []
            for record in rows[:QUERY_MAX_COUNT]:
                item = self._accepted(record, scope, pending)
                if item is not None:
                    accepted.append(item)
            for stamp, event_id, text in sorted(accepted, key=lambda item: item[0]):
                if event_id in self._seen:
                    continue
                self._seen[event_id] = None
                while len(self._seen) > MAX_EVENT_REFS:
                    self._seen.popitem(last=False)
                self._sequence += 1
                self._replies.append((stamp, self._sequence, self._monotonic(), text))
            self._replies.sort(key=lambda item: (item[0], item[1]))
            self._replies = self._replies[-MAX_REPLY_VALUES:]
        except asyncio.CancelledError:
            raise
        except Exception:
            # An old host, a failed RPC, or malformed observation must not stop
            # a live reply. Do not log response bodies or private host text.
            return

    def _accepted(
        self, record: object, scope: ReplyScope, pending: frozenset[str],
    ) -> tuple[float, str, str] | None:
        if _field(record, "lanlan_name") != scope.target:
            return None
        turn_type = _field(record, "turn_type")
        if not isinstance(turn_type, str) or turn_type not in {"assistant_message", "proactive_reply"}:
            return None
        metadata = _field(record, "metadata")
        if not isinstance(metadata, Mapping) or metadata.get("role") != "cat":
            return None
        event_id = metadata.get("event_id")
        if not _opaque_id(event_id):
            return None
        provenance = metadata.get("reply_provenance")
        if not isinstance(provenance, Mapping):
            return None
        if type(provenance.get("version")) is not int or provenance.get("version") != 1:
            return None
        if provenance.get("complete") is not True:
            return None
        sources = provenance.get("sources")
        if not isinstance(sources, list) or not 0 < len(sources) <= MAX_PROVENANCE_SOURCES:
            return None
        for source in sources:
            if not isinstance(source, Mapping) or source.get("kind") != "plugin":
                return None
            if source.get("plugin_id") != "neko_live":
                return None
            request_id = source.get("request_id")
            if not _opaque_id(request_id) or request_id not in pending:
                return None
        content = _field(record, "content")
        if not isinstance(content, str):
            return None
        snippets = compact_recent_reply_values([content], limit=1, text_limit=MAX_REPLY_CHARS)
        if not snippets:
            return None
        return _timestamp(record, metadata), event_id, snippets[0]
