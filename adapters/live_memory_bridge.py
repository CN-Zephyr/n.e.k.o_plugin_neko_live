"""Send live viewer turns into N.E.K.O scoped memory.

The wire shape matches ``qq_auto_reply`` ``post_scoped_memory_history``:
one viewer is a ``participant`` subject, and ``speaker_label`` is required so
extraction does not file the line as a fact about the owner.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_BATCH_MESSAGES = 6


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
) -> dict[str, Any]:
    label = (nickname or uid)[:64]
    payload: dict[str, Any] = {
        "input_history": json.dumps(messages, ensure_ascii=False),
        "subject": {
            "subject_kind": "participant",
            "subject_id": f"bilibili:{uid}",
        },
        "speaker_label": label,
        "speaker_id": f"bilibili:{uid}"[:96],
        "display_name": label,
        "speaker_channel": "bilibili",
        "speaker_tier": speaker_tier_for_guard(guard_level, medal_level),
        "idempotency_key": _batch_key(uid, messages),
    }
    activity = _activity_events(uid, messages)
    if activity:
        payload["speaker_activity_events"] = activity
    return payload


class LiveMemoryBridge:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._buckets: dict[str, list[dict[str, str]]] = {}
        self._names: dict[str, str] = {}
        self._guards: dict[str, int] = {}
        self._medals: dict[str, int] = {}
        self._posted_keys: set[str] = set()
        self._unavailable_reason = ""

    def note_exchange(
        self,
        *,
        uid: str,
        nickname: str,
        user_text: str,
        assistant_text: str,
        guard_level: int = 0,
        medal_level: int = 0,
    ) -> None:
        safe_uid = str(uid or "").strip()
        user_line = " ".join(str(user_text or "").split())
        if not safe_uid or not user_line:
            return
        bucket = self._buckets.setdefault(safe_uid, [])
        bucket.append({"role": "user", "content": user_line[:500]})
        reply = " ".join(str(assistant_text or "").split())
        if reply:
            bucket.append({"role": "assistant", "content": reply[:500]})
        if nickname:
            self._names[safe_uid] = " ".join(str(nickname).split())[:64]
        if guard_level > self._guards.get(safe_uid, 0):
            self._guards[safe_uid] = int(guard_level)
        if medal_level > self._medals.get(safe_uid, 0):
            self._medals[safe_uid] = int(medal_level)
        if len(bucket) >= _BATCH_MESSAGES:
            self._schedule(safe_uid)

    def _schedule(self, uid: str) -> None:
        messages = self._buckets.pop(uid, [])
        if not messages:
            return
        nickname = self._names.get(uid, uid)
        try:
            import asyncio

            asyncio.get_running_loop().create_task(self.post_viewer(uid, nickname, messages))
        except RuntimeError:
            self._buckets[uid] = messages
            return

    async def flush(self) -> None:
        pending = list(self._buckets.items())
        self._buckets.clear()
        for uid, messages in pending:
            if messages:
                await self.post_viewer(uid, self._names.get(uid, uid), messages)

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

    async def post_viewer(self, uid: str, nickname: str, messages: list[dict[str, str]]) -> bool:
        her_name = ""
        dispatcher = getattr(self.runtime, "dispatcher", None)
        target = getattr(dispatcher, "live_target_lanlan", None)
        if callable(target):
            her_name = str(target() or "").strip()
        if not her_name or not messages:
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
        payload = scoped_history_payload(
            uid=uid,
            nickname=nickname,
            messages=messages,
            guard_level=self._guards.get(uid, 0),
            medal_level=self._medals.get(uid, 0),
        )
        batch_key = str(payload.get("idempotency_key") or "")
        if batch_key and batch_key in self._posted_keys:
            return True
        try:
            client = get_internal_http_client()
            response = await client.post(
                f"http://127.0.0.1:{port}/internal/memory/{her_name}/scoped_history",
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
        except Exception as exc:
            self._remember_unavailable(type(exc).__name__)
            pending = self._buckets.get(uid, [])
            self._buckets[uid] = list(messages) + pending
            return False
        if batch_key:
            self._posted_keys.add(batch_key)
        return True

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
