"""Optional entry, follow, and like speech. Counting stays on the audience session."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .._base import BaseModule
from ..live_events.provider_event import (
    event_is_current_session,
    event_nickname,
    event_room_id,
    event_room_ref,
    event_session_generation,
    event_text,
    event_type,
    event_uid,
)


def interact_kind(event: Any) -> str:
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        kind = str(payload.get("interact_kind") or "").strip().lower()
        if kind in {"entry", "follow", "share"}:
            return kind
    raw = getattr(event, "raw", None)
    extra = getattr(raw, "extra_json", "") if raw is not None else ""
    if isinstance(extra, str) and extra:
        try:
            data = json.loads(extra)
            code = int((data.get("data") or {}).get("msg_type") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            code = 0
        if code == 2:
            return "follow"
        if code == 3:
            return "share"
        if code == 1:
            return "entry"
    name = str(getattr(getattr(raw, "msg_type", None), "name", "") or "")
    if name == "MSG_ATTENTION":
        return "follow"
    return "entry"


class LivePresenceModule(BaseModule):
    """Subscribe to entry and like. Speech stays off unless a switch is on."""

    id = "live_presence"
    title = "Live presence"
    domain = "interaction"

    def __init__(self) -> None:
        super().__init__()
        self._unsubscribes: list[Any] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._like_idle_seconds = 1.0
        self._like_buckets: dict[tuple[str, str], dict[str, Any]] = {}
        self._like_generation: dict[tuple[str, str], int] = {}
        self._like_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._gate_drop_counts: dict[str, int] = {}
        self._last_gate_reason = ""

    async def setup(self, ctx: Any) -> None:
        await super().setup(ctx)
        bus = getattr(ctx, "event_bus", None)
        if bus is None:
            return
        for event_name in ("entry", "like"):
            self._unsubscribes.append(bus.subscribe(event_name, self._on_bus_event, owner=self.id))

    async def teardown(self) -> None:
        for unsubscribe in self._unsubscribes:
            if callable(unsubscribe):
                unsubscribe()
        self._unsubscribes = []
        self.reset()
        pending = [task for task in list(self._tasks) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        await super().teardown()

    def reset(self) -> None:
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._like_generation.clear()
        for task in list(self._like_tasks.values()):
            if not task.done():
                task.cancel()
        self._like_tasks.clear()
        self._like_buckets.clear()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "subscribed": bool(self._unsubscribes),
            "last_gate_reason": self._last_gate_reason,
            "gate_drop_counts": dict(self._gate_drop_counts),
        }

    def _drop_gate(self, reason: str) -> None:
        self._last_gate_reason = reason
        self._gate_drop_counts[reason] = self._gate_drop_counts.get(reason, 0) + 1

    def _on_bus_event(self, event: Any) -> None:
        if not self.enabled:
            self._drop_gate("module_disabled")
            return
        if self.ctx is None:
            self._drop_gate("no_context")
            return
        if not event_is_current_session(event, self.ctx):
            self._drop_gate("stale_session")
            return
        kind = event_type(event)
        if kind == "like":
            self._on_like(event)
            return
        if kind == "entry":
            self._on_entry(event)

    def _payload(self, event: Any, event_type_hint: str) -> dict[str, Any]:
        payload = {
            "uid": event_uid(event),
            "nickname": event_nickname(event),
            "danmaku_text": event_text(event),
            "room_id": event_room_id(event),
            "event_type": event_type_hint,
        }
        room_ref = event_room_ref(event)
        if room_ref:
            payload["room_ref"] = room_ref
        generation = event_session_generation(event)
        if generation:
            payload["_live_session_generation"] = generation
        return payload

    def _on_like(self, event: Any) -> None:
        if not bool(getattr(self.ctx.config, "like_greet_enabled", False)):
            self._drop_gate("like_speech_disabled")
            return
        payload = self._payload(event, "like")
        if not payload.get("uid"):
            self._drop_gate("unverified_or_missing_uid")
            return
        self._buffer_like(payload)

    def _on_entry(self, event: Any) -> None:
        config = self.ctx.config
        interact = interact_kind(event)
        payload = self._payload(event, "entry")
        if not payload.get("uid"):
            self._drop_gate("unverified_or_missing_uid")
            return
        if interact == "follow":
            if not bool(getattr(config, "follow_greet_enabled", False)):
                self._drop_gate("follow_speech_disabled")
                return
            payload["event_type"] = "follow"
            payload["danmaku_text"] = payload.get("danmaku_text") or "关注了主播"
            payload["support_verified"] = True
            payload["support_evidence"] = "bilibili_typed_command"
            payload["gift_name"] = "follow"
            self._schedule(payload)
            return
        if bool(getattr(config, "entry_roast_enabled", False)):
            payload["presence_action"] = "roast"
            payload["danmaku_text"] = payload.get("danmaku_text") or "刚进直播间"
            self._schedule(payload)
            return
        if bool(getattr(config, "entry_greet_enabled", False)):
            payload["presence_action"] = "greet"
            payload["event_type"] = "entry"
            payload["danmaku_text"] = "来了"
            self._schedule(payload)
            return
        self._drop_gate("presence_speech_disabled")

    def _buffer_like(self, payload: dict[str, Any]) -> None:
        cross_viewer = bool(getattr(self.ctx.config, "like_greet_cross_viewer", False))
        room = str(payload.get("room_ref") or payload.get("room_id") or "").strip()
        uid = str(payload.get("uid") or "").strip()
        key = (room, "" if cross_viewer else uid)
        bucket = self._like_buckets.get(key)
        if bucket is None:
            bucket = dict(payload)
            bucket["event_type"] = "like"
            bucket["gift_name"] = "like"
            bucket["gift_num"] = 0
            bucket["support_verified"] = True
            bucket["support_evidence"] = "bilibili_typed_command"
            self._like_buckets[key] = bucket
        count = int(bucket.get("gift_num") or 0) + 1
        bucket["gift_num"] = count
        nickname = str(payload.get("nickname") or "").strip()
        if nickname:
            bucket["nickname"] = nickname
        previous = self._like_tasks.get(key)
        if previous is not None and not previous.done():
            previous.cancel()
        generation = self._like_generation.get(key, 0) + 1
        self._like_generation[key] = generation
        try:
            task = asyncio.get_running_loop().create_task(self._flush_like(key, generation))
        except RuntimeError:
            self._drop_gate("no_event_loop")
            return
        self._like_tasks[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush_like(self, key: tuple[str, str], generation: int) -> None:
        try:
            await asyncio.sleep(self._like_idle_seconds)
        except asyncio.CancelledError:
            return
        if self._like_generation.get(key) != generation:
            return
        bucket = self._like_buckets.pop(key, None)
        self._like_generation.pop(key, None)
        self._like_tasks.pop(key, None)
        if not bucket or self.ctx is None:
            return
        if not bool(getattr(self.ctx.config, "like_greet_enabled", False)):
            self._drop_gate("like_speech_disabled")
            return
        minimum = int(getattr(self.ctx.config, "like_greet_min_count", 5) or 5)
        total = int(bucket.get("gift_num") or 0)
        if total < minimum:
            self._drop_gate("like_below_minimum")
            return
        bucket["danmaku_text"] = f"点赞了 {total} 次"
        if key[1] == "":
            bucket["uid"] = "room_likes"
            bucket["nickname"] = ""
            bucket["like_scope"] = "room"
        self._schedule(bucket)

    def _schedule(self, payload: dict[str, Any]) -> None:
        handle = getattr(self.ctx, "handle_live_payload", None)
        if not callable(handle):
            self._drop_gate("no_event_loop")
            return
        try:
            task = asyncio.get_running_loop().create_task(handle(payload))
        except RuntimeError:
            self._drop_gate("no_event_loop")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
