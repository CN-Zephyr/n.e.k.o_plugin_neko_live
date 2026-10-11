from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.adapters.live_memory_bridge import (
    LiveMemoryBridge,
    scoped_history_payload,
)
from plugin.plugins.neko_live.core.contracts import LiveConfig, LiveEvent, ViewerEvent
from plugin.plugins.neko_live.core.event_bus import EventBus
from plugin.plugins.neko_live.core.pipeline_routing import route_for_event
from plugin.plugins.neko_live.modules.bili_live_ingest.livedanmaku import LiveDanmaku
from plugin.plugins.neko_live.modules.live_audience_session import LiveAudienceSessionModule
from plugin.plugins.neko_live.modules.live_presence import LivePresenceModule
from plugin.plugins.neko_live.modules.live_support_events.scheduler import SupportEventScheduler
from plugin.plugins.neko_live.stores.audit_store import AuditStore


def test_scoped_history_payload_keeps_viewer_speech_off_the_owner():
    payload = scoped_history_payload(
        uid="7",
        nickname="小鱼",
        messages=[{"role": "user", "content": "我175"}],
    )

    assert payload["subject"] == {"subject_kind": "participant", "subject_id": "bilibili:7"}
    assert payload["speaker_label"] == "小鱼"
    assert payload["speaker_id"] == "bilibili:7"
    assert payload["speaker_tier"] == "normal"
    assert payload["speaker_activity_events"][0]["id"].startswith("activity_")
    assert str(payload["idempotency_key"]).startswith("live_")
    assert "我175" in payload["input_history"]
    guarded = scoped_history_payload(
        uid="7",
        nickname="小鱼",
        messages=[{"role": "user", "content": "我175"}],
        guard_level=3,
    )
    assert guarded["speaker_tier"] == "trusted"
    medaled = scoped_history_payload(
        uid="7",
        nickname="小鱼",
        messages=[{"role": "user", "content": "我175"}],
        medal_level=16,
    )
    assert medaled["speaker_tier"] == "trusted"


@pytest.mark.asyncio
async def test_memory_bridge_flushes_pending_viewer_turns():
    posted: list[tuple[str, str, list[dict[str, str]]]] = []
    bridge = LiveMemoryBridge(SimpleNamespace())

    async def fake_post(uid: str, nickname: str, messages: list[dict[str, str]]) -> bool:
        posted.append((uid, nickname, messages))
        return True

    bridge.post_viewer = fake_post
    bridge.note_exchange(uid="7", nickname="小鱼", user_text="我175", assistant_text="记住了")
    assert posted == []

    await bridge.flush()

    assert posted[0][0] == "7"
    assert posted[0][1] == "小鱼"
    assert posted[0][2][0]["content"] == "我175"


def test_entry_greet_routes_without_marking_roasted():
    event = ViewerEvent(
        uid="7",
        nickname="小鱼",
        danmaku_text="来了",
        source="live_danmaku",
        raw={"event_type": "entry", "presence_action": "greet"},
    )

    route = route_for_event(
        event,
        is_transient_event_result=False,
        has_uid_lock=True,
        already_roasted=False,
        entrance_pacing_active=False,
    )

    assert route.response_module_id == "danmaku_response"
    assert route.viewer_gate_reason == "entry_greet"
    assert route.should_mark_roasted is False


def test_entry_roast_switch_sends_text_danmaku_to_reply():
    event = ViewerEvent(
        uid="7",
        nickname="小鱼",
        danmaku_text="猫猫在吗",
        source="live_danmaku",
        raw={},
    )

    kept = route_for_event(
        event,
        is_transient_event_result=False,
        has_uid_lock=True,
        already_roasted=False,
        entrance_pacing_active=False,
    )
    moved = route_for_event(
        event,
        is_transient_event_result=False,
        has_uid_lock=True,
        already_roasted=False,
        entrance_pacing_active=False,
        entry_roast_owns_danmaku=True,
    )

    assert kept.response_module_id == "avatar_roast"
    assert moved.response_module_id == "danmaku_response"
    assert moved.viewer_gate_reason == "entry_roast_owns_danmaku"
    assert moved.should_mark_roasted is False


def test_like_light_aggregation_key_groups_one_viewer():
    key = SupportEventScheduler._light_aggregation_key(
        {"event_type": "like", "room_id": "42", "uid": "7"}
    )
    assert key == ("42", "7", "like")


def test_like_total_reads_click_count_without_adding():
    event = LiveDanmaku.from_like_total({"data": {"click_count": 8801}})
    assert event.click_count == 8801


@pytest.mark.asyncio
async def test_audience_panel_counts_entry_follow_like_and_total():
    bus = EventBus(AuditStore())
    module = LiveAudienceSessionModule()
    module._now = lambda: 1_700_000_000.0
    await module.setup(SimpleNamespace(event_bus=bus, config=LiveConfig()))
    module.start_session()

    bus.publish("entry", LiveEvent(type="entry", uid="1", payload={"interact_kind": "entry"}))
    bus.publish("entry", LiveEvent(type="entry", uid="2", payload={"interact_kind": "follow"}))
    bus.publish("entry", LiveEvent(type="entry", uid="3", payload={"interact_kind": "share"}))
    bus.publish("like", LiveEvent(type="like", uid="1", payload={}))
    bus.publish("like_total", LiveEvent(type="like_total", payload={"click_count": 88}))

    snapshot = module.snapshot()
    assert snapshot["entry_count"] == 1
    assert snapshot["follow_count"] == 1
    assert snapshot["like_count"] == 1
    assert snapshot["like_total"] == 88
    assert snapshot["danmaku_count"] == 0


@pytest.mark.asyncio
async def test_like_and_entry_stay_silent_unless_enabled():
    bus = EventBus(AuditStore())
    module = LivePresenceModule()
    called: list[dict] = []

    async def handle_live_payload(payload: dict) -> None:
        called.append(payload)

    await module.setup(
        SimpleNamespace(
            event_bus=bus,
            config=LiveConfig(),
            audit=None,
            handle_live_payload=handle_live_payload,
        )
    )

    bus.publish("like", LiveEvent(type="like", uid="9", payload={"uid": "9", "room_id": 1, "nickname": "a"}))
    bus.publish(
        "entry",
        LiveEvent(type="entry", uid="9", payload={"uid": "9", "nickname": "a", "interact_kind": "entry"}),
    )
    await asyncio.sleep(0)

    assert called == []
    assert module._last_gate_reason == "presence_speech_disabled"


@pytest.mark.asyncio
async def test_like_burst_thanks_once_after_idle():
    bus = EventBus(AuditStore())
    module = LivePresenceModule()
    module._like_idle_seconds = 0.01
    called: list[dict] = []

    async def handle_live_payload(payload: dict) -> None:
        called.append(dict(payload))

    await module.setup(
        SimpleNamespace(
            event_bus=bus,
            config=LiveConfig(like_greet_enabled=True, like_greet_min_count=3),
            audit=None,
            handle_live_payload=handle_live_payload,
        )
    )

    for _ in range(3):
        bus.publish(
            "like",
            LiveEvent(
                type="like",
                uid="9",
                payload={"uid": "9", "room_id": 1, "nickname": "a", "event_type": "like"},
            ),
        )
    await asyncio.sleep(0.08)

    assert len(called) == 1
    assert called[0]["event_type"] == "like"
    assert called[0]["gift_num"] == 3
    assert called[0]["support_verified"] is True


@pytest.mark.asyncio
async def test_cross_viewer_likes_thank_the_room_once():
    bus = EventBus(AuditStore())
    module = LivePresenceModule()
    module._like_idle_seconds = 0.01
    called: list[dict] = []

    async def handle_live_payload(payload: dict) -> None:
        called.append(dict(payload))

    await module.setup(
        SimpleNamespace(
            event_bus=bus,
            config=LiveConfig(
                like_greet_enabled=True,
                like_greet_min_count=2,
                like_greet_cross_viewer=True,
            ),
            audit=None,
            handle_live_payload=handle_live_payload,
        )
    )
    bus.publish(
        "like",
        LiveEvent(type="like", uid="9", payload={"uid": "9", "room_id": 1, "nickname": "a"}),
    )
    bus.publish(
        "like",
        LiveEvent(type="like", uid="8", payload={"uid": "8", "room_id": 1, "nickname": "b"}),
    )
    await asyncio.sleep(0.08)

    assert len(called) == 1
    assert called[0]["uid"] == "room_likes"
    assert called[0]["nickname"] == ""
    assert called[0]["like_scope"] == "room"
    assert called[0]["gift_num"] == 2


@pytest.mark.asyncio
async def test_entry_greet_schedules_one_presence_payload():
    bus = EventBus(AuditStore())
    module = LivePresenceModule()
    called: list[dict] = []

    async def handle_live_payload(payload: dict) -> None:
        called.append(payload)

    await module.setup(
        SimpleNamespace(
            event_bus=bus,
            config=LiveConfig(entry_greet_enabled=True),
            audit=None,
            handle_live_payload=handle_live_payload,
        )
    )
    bus.publish(
        "entry",
        LiveEvent(
            type="entry",
            uid="9",
            payload={"uid": "9", "nickname": "小鱼", "interact_kind": "entry", "room_id": 1},
        ),
    )
    await asyncio.sleep(0)

    assert called[0]["presence_action"] == "greet"
    assert called[0]["danmaku_text"] == "来了"
    assert "support_verified" not in called[0]
