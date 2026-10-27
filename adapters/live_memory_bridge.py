"""Send live viewer turns into N.E.K.O scoped memory.

The wire shape matches ``qq_auto_reply`` ``post_scoped_memory_history``:
one viewer is a ``participant`` subject, and ``speaker_label`` is required so
extraction does not file the line as a fact about the owner.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

_BATCH_MESSAGES = 6
_MAX_BUCKETS = 256
_MAX_TASKS = 8
_MAX_POSTED_KEYS = 1024
_MAX_PENDING_MESSAGES = 12
_MAX_CACHED_EPOCHS = 512


@dataclass(frozen=True)
class MemoryScope:
    target: str
    platform: str
    session: int
    uid: str


def _subject_id(platform: str, uid: str) -> str:
    prefix = platform + ":"
    return uid if uid.startswith(prefix) else prefix + uid


def subject_key(platform: str, uid: str) -> str:
    """``MemorySubject.key`` of a viewer: the key ``subject_epochs`` is indexed by."""
    return "participant:" + _subject_id(platform, uid)


def _batch_key(uid: str, messages: list[dict[str, str]]) -> str:
    raw = json.dumps({"uid": uid, "messages": messages}, ensure_ascii=False, sort_keys=True)
    return "live_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def speaker_tier_for_guard(guard_level: int, medal_level: int = 0) -> str:
    if guard_level >= 1 or medal_level >= 1:
        return "trusted"
    return "normal"


def _activity_events(uid: str, messages: list[dict[str, str]]) -> list[dict[str, object]]:
    seen: dict[str, int] = {}
    events: list[dict[str, object]] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        digest = hashlib.sha256(
            f"{uid}|{message.get('content', '')}".encode("utf-8", errors="ignore")
        ).hexdigest()[:24]
        occurrence = seen.get(digest, 0)
        seen[digest] = occurrence + 1
        suffix = "" if occurrence == 0 else f".{occurrence}"
        events.append({"id": f"activity_{digest}{suffix}", "count": 1})
    return events


def scoped_history_payload(
    *,
    uid: str,
    nickname: str,
    messages: list[dict[str, str]],
    guard_level: int = 0,
    medal_level: int = 0,
    platform: str = "bilibili",
    scope_key: str = "",
    subject_epochs: dict[str, int] | None = None,
) -> dict[str, Any]:
    label = (nickname or uid)[:64]
    payload: dict[str, Any] = {
        "input_history": json.dumps(messages, ensure_ascii=False),
        "subject": {
            "subject_kind": "participant",
            "subject_id": _subject_id(platform, uid),
        },
        "speaker_label": label,
        "speaker_id": _subject_id(platform, uid)[:96],
        "display_name": label,
        "speaker_channel": platform,
        "speaker_tier": speaker_tier_for_guard(guard_level, medal_level),
        "idempotency_key": _batch_key(scope_key or _subject_id(platform, uid), messages),
    }
    activity = _activity_events(scope_key or _subject_id(platform, uid), messages)
    if activity:
        payload["speaker_activity_events"] = activity
    if subject_epochs is not None:
        payload["subject_epochs"] = dict(subject_epochs)
    return payload


class LiveMemoryBridge:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._buckets: OrderedDict[MemoryScope, list[dict[str, str]]] = OrderedDict()
        self._metadata: dict[MemoryScope, tuple[str, int, int]] = {}
        self._tasks: dict[asyncio.Task, MemoryScope] = {}
        self._posted_keys: OrderedDict[str, None] = OrderedDict()
        self._epoch_cache: OrderedDict[str, int] = OrderedDict()
        self._unavailable_reason = ""
        self._closed = False

    def _enabled(self) -> bool:
        return not self._closed and getattr(getattr(self.runtime, "config", None), "viewer_memory_enabled", False) is True

    def discard(self, uid: str | None = None) -> None:
        for scope in list(self._buckets):
            if uid is None or scope.uid == uid:
                self._buckets.pop(scope, None)
                self._metadata.pop(scope, None)
        for task, scope in list(self._tasks.items()):
            if uid is None or scope.uid == uid:
                task.cancel()
        if uid is None:
            self._posted_keys.clear()
            self._epoch_cache.clear()

    def note_exchange(
        self, *, uid: str, nickname: str, user_text: str, assistant_text: str = "",
        guard_level: int = 0, medal_level: int = 0, platform: str | None = None,
    ) -> None:
        if not self._enabled():
            self.discard()
            return
        target = getattr(getattr(self.runtime, "dispatcher", None), "live_target_lanlan", None)
        her_name = str(target() or "").strip() if callable(target) else ""
        provider = getattr(self.runtime, "live_provider", None)
        platform = platform or str(getattr(provider, "platform", "") or "")
        safe_uid = str(uid or "").strip()
        user_line = " ".join(str(user_text or "").split())
        if not her_name or platform not in {"bilibili", "twitch", "douyin"} or not safe_uid or not user_line:
            return
        scope = MemoryScope(her_name, platform, int(getattr(self.runtime, "_live_session_generation", 0)), safe_uid)
        if scope not in self._buckets and len(self._buckets) >= _MAX_BUCKETS:
            old, _ = self._buckets.popitem(last=False)
            self._metadata.pop(old, None)
        bucket = self._buckets.setdefault(scope, [])
        bucket.append({"role": "user", "content": user_line[:500]})
        del bucket[:-_MAX_PENDING_MESSAGES]
        previous = self._metadata.get(scope, (safe_uid, 0, 0))
        self._metadata[scope] = (
            " ".join(str(nickname or previous[0]).split())[:64],
            max(previous[1], int(guard_level)), max(previous[2], int(medal_level)),
        )
        # Submission receipts are not generated assistant speech.
        if len(bucket) >= _BATCH_MESSAGES:
            self._schedule(scope)

    def _schedule(self, scope: MemoryScope) -> None:
        if len(self._tasks) >= _MAX_TASKS or not self._buckets.get(scope):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        messages = self._buckets.pop(scope)
        metadata = self._metadata.pop(scope, (scope.uid, 0, 0))
        task = loop.create_task(self._submit(scope, messages, metadata))
        self._tasks[task] = scope
        task.add_done_callback(self._tasks.pop)

    async def _submit(self, scope: MemoryScope, messages: list[dict[str, str]], metadata: tuple[str, int, int]) -> None:
        try:
            saved = await self.post_viewer(scope.uid, metadata[0], messages, scope=scope, guard_level=metadata[1], medal_level=metadata[2])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._remember_unavailable(type(exc).__name__)
            saved = False
        if saved is False and self._enabled():
            if scope in self._buckets or len(self._buckets) < _MAX_BUCKETS:
                self._buckets[scope] = (messages + self._buckets.get(scope, []))[-_MAX_PENDING_MESSAGES:]
                self._metadata[scope] = metadata

    async def flush(self) -> None:
        if not self._enabled():
            self.discard()
        else:
            pending = list(self._buckets)
            for scope in pending:
                if len(self._tasks) >= _MAX_TASKS:
                    await asyncio.gather(*list(self._tasks), return_exceptions=True)
                self._schedule(scope)
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.flush(), timeout=2.0)
        finally:
            self._closed = True
            self.discard()
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _memory_port(self) -> int:
        try:
            from config import MEMORY_SERVER_PORT

            return int(MEMORY_SERVER_PORT)
        except Exception:
            pass
        ctx = getattr(self.runtime, "ctx", None)
        getter = getattr(ctx, "get_system_config", None)
        if not callable(getter):
            return 0
        try:
            payload = await getter(timeout=2.0)
        except Exception:
            return 0
        config = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(config, dict):
            return 0
        try:
            return int(config.get("MEMORY_SERVER_PORT") or 0)
        except (TypeError, ValueError):
            return 0

    async def post_viewer(
        self, uid: str, nickname: str, messages: list[dict[str, str]], *,
        scope: MemoryScope, guard_level: int = 0, medal_level: int = 0,
    ) -> bool | None:
        """Submit one batch: True when saved, False to retry later, None when rejected for good."""
        if not self._enabled() or not messages:
            return False
        port = await self._memory_port()
        if port <= 0:
            self._remember_unavailable("missing_port")
            return False
        try:
            from utils.internal_http_client import get_internal_http_client
        except Exception as exc:
            self._remember_unavailable(type(exc).__name__)
            return False
        scope_key = json.dumps([scope.target, scope.platform, scope.session, scope.uid])
        batch_key = _batch_key(scope_key, messages)
        if batch_key in self._posted_keys:
            return True
        base_url = f"http://127.0.0.1:{port}/internal/memory/{quote(scope.target, safe='')}"
        key = subject_key(scope.platform, uid)
        client = get_internal_http_client()
        # 带键请求必须给出 subject 的清除代数，且同一个键的每次重试都要原样带回：
        # 请求哈希覆盖 subject_epochs，代数一变服务端就按「键被另一个请求复用」拒绝
        epoch = self._epoch_cache.get(batch_key)
        if epoch is None:
            epoch = await self._forget_epoch(client, base_url, key)
            if epoch is None:
                return False
            self._epoch_cache[batch_key] = epoch
            if len(self._epoch_cache) > _MAX_CACHED_EPOCHS:
                self._epoch_cache.popitem(last=False)
        payload = scoped_history_payload(
            uid=uid,
            nickname=nickname,
            messages=messages,
            guard_level=guard_level,
            medal_level=medal_level,
            platform=scope.platform,
            scope_key=scope_key,
            subject_epochs={key: epoch},
        )
        if not self._enabled():
            return False
        try:
            response = await client.post(f"{base_url}/scoped_history", json=payload, timeout=30.0)
            if getattr(response, "status_code", None) == 422:
                # 请求本身被拒（键已被另一请求占用、字段不合法）：同键重试只会一直 422，丢弃这一批
                self._epoch_cache.pop(batch_key, None)
                self._remember_unavailable("memory_request_rejected")
                return None
            response.raise_for_status()
            acknowledgement = response.json()
            trust = acknowledgement.get("trust") if isinstance(acknowledgement, dict) else None
            if (
                not isinstance(acknowledgement, dict)
                or acknowledgement.get("status") != "processed"
                or not isinstance(trust, dict)
                or "persisted" not in trust
                or not (trust["persisted"] is True or trust["persisted"] is None)
            ):
                self._remember_unavailable("memory_ack_unconfirmed")
                return False
        except Exception as exc:
            self._remember_unavailable(type(exc).__name__)
            return False
        self._epoch_cache.pop(batch_key, None)
        self._posted_keys[batch_key] = None
        if len(self._posted_keys) > _MAX_POSTED_KEYS:
            self._posted_keys.popitem(last=False)
        return True

    async def _forget_epoch(self, client: Any, base_url: str, key: str) -> int | None:
        """Server-side forget fence of ``key``; ``None`` when it cannot be read.

        A key without a tombstone is absent from the answer and means 0. An
        unreadable fence (503, transport error, malformed body) must not read
        as 0, so the batch waits for a later flush instead.
        """
        try:
            response = await client.get(
                f"{base_url}/forget_epochs", params=[("subject", key)], timeout=5.0,
            )
            response.raise_for_status()
            body = response.json()
            epochs = body.get("epochs") if isinstance(body, dict) else None
            if not isinstance(epochs, dict):
                raise ValueError("malformed epochs")
            value = epochs.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("malformed epoch")
            return value
        except asyncio.CancelledError:
            raise
        except Exception:
            self._remember_unavailable("forget_epochs_unavailable")
            return None

    def _remember_unavailable(self, reason: str) -> None:
        if self._unavailable_reason == reason:
            return
        self._unavailable_reason = reason
        audit = getattr(self.runtime, "audit", None)
        record = getattr(audit, "record", None)
        if callable(record):
            record(
                "live_memory_unavailable",
                "scoped viewer memory was not submitted",
                level="info",
                detail={"reason": reason[:80]},
            )
