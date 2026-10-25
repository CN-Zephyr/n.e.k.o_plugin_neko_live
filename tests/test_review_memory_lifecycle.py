from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.neko_live.adapters.live_memory_bridge import LiveMemoryBridge, scoped_history_payload
from plugin.plugins.neko_live.core.contracts import LiveConfig, ViewerEvent, ViewerIdentity
from plugin.plugins.neko_live.core.event_bus import EventBus
from plugin.plugins.neko_live.core.module_registry_lifecycle import teardown_all_modules
from plugin.plugins.neko_live.core.pipeline_viewers import resolve_viewer_context
from plugin.plugins.neko_live.core.runtime_live_input import _note_live_memory
from plugin.plugins.neko_live.core.viewer_utterance import viewer_utterance_text


def memory_runtime():
    return SimpleNamespace(
        config=LiveConfig(), dispatcher=SimpleNamespace(live_target_lanlan=lambda: "A"),
        live_provider=SimpleNamespace(platform="twitch"), _live_session_generation=1,
    )


def epochs_get(*answers):
    """Fake ``GET forget_epochs``: each call pops the next answer; the last one repeats."""
    queue = list(answers) or [{"epochs": {}}]
    calls = []

    async def get(url, *, params, **_kwargs):
        calls.append((url, params))
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        status = answer if isinstance(answer, int) else 200

        def raise_for_status():
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

        return SimpleNamespace(status_code=status, raise_for_status=raise_for_status, json=lambda: answer)

    get.calls = calls
    return get


@pytest.mark.asyncio
async def test_memory_preserves_collection_scope_and_excludes_submission_receipt():
    runtime = memory_runtime()
    _note_live_memory(runtime, {"status": "pushed", "output": "queued_to_neko(...)", "event": {"uid": "twitch:7", "danmaku_text": "hello", "source": "live_danmaku"}})
    runtime.dispatcher.live_target_lanlan = lambda: "B"
    runtime.live_provider.platform = "douyin"
    runtime._live_session_generation = 2
    submitted = []

    async def post(uid, nickname, messages, **kwargs):
        submitted.append((uid, messages, kwargs["scope"]))
        return True

    runtime.live_memory.post_viewer = post
    await runtime.live_memory.flush()
    uid, messages, scope = submitted[0]
    assert (scope.target, scope.platform, scope.session) == ("A", "twitch", 1)
    assert messages == [{"role": "user", "content": "hello"}]
    payload = scoped_history_payload(uid=uid, nickname="viewer", messages=messages, platform=scope.platform)
    assert payload["subject"]["subject_id"] == "twitch:7"
    assert payload["speaker_channel"] == "twitch"


@pytest.mark.asyncio
async def test_disabled_memory_does_not_collect_or_flush_previous_turns():
    runtime = memory_runtime()
    bridge = LiveMemoryBridge(runtime)
    bridge.note_exchange(uid="7", nickname="viewer", user_text="hello")
    runtime.config.viewer_memory_enabled = False
    await bridge.flush()
    assert not bridge._buckets
    assert not bridge._tasks
    _note_live_memory(runtime, {"status": "pushed", "event": {"uid": "7", "danmaku_text": "hello"}})
    assert not hasattr(runtime, "live_memory")


@pytest.mark.asyncio
async def test_memory_flush_awaits_full_batch_and_close_cancels_owned_request():
    bridge = LiveMemoryBridge(memory_runtime())
    entered, released, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def post(*args, **kwargs):
        entered.set()
        try:
            await released.wait()
        finally:
            cancelled.set()
        return True

    bridge.post_viewer = post
    for i in range(6):
        bridge.note_exchange(uid="7", nickname="viewer", user_text=str(i))
    await entered.wait()
    flushing = asyncio.create_task(bridge.flush())
    await asyncio.sleep(0)
    assert not flushing.done()
    released.set()
    await flushing
    assert not bridge._tasks
    released.clear()
    for i in range(6):
        bridge.note_exchange(uid="8", nickname="viewer", user_text=str(i))
    closing = asyncio.create_task(bridge.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert cancelled.is_set()
    assert not bridge._tasks and not bridge._buckets


def test_memory_pending_capacity_is_bounded():
    bridge = LiveMemoryBridge(memory_runtime())
    for i in range(300):
        bridge.note_exchange(uid=str(i), nickname="viewer", user_text="hello")
    assert len(bridge._buckets) == len(bridge._metadata) == 256


@pytest.mark.asyncio
async def test_event_bus_outer_timeout_cancels_handler():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(_event):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bus = EventBus()
    bus.subscribe("message", handler)
    bus.publish("message", {})
    await entered.wait()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bus.close(), timeout=0.01)
    assert cancelled.is_set()
    assert bus.status()["pending_tasks"] == 0


@pytest.mark.asyncio
async def test_module_timeout_does_not_skip_sibling_cleanup():
    cleaned = []

    async def slow():
        cleaned.append("slow")
        await asyncio.Event().wait()

    async def fast():
        cleaned.append("fast")

    modules = {"fast": SimpleNamespace(id="fast", teardown=fast), "slow": SimpleNamespace(id="slow", teardown=slow)}
    with pytest.raises(RuntimeError, match="incomplete"):
        await teardown_all_modules(modules, timeout=0.01)
    assert cleaned == ["slow", "fast"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["entry", "follow", "like", "gift", "guard"])
async def test_system_events_never_learn_as_viewer_utterances(runtime, kind):
    runtime.bili_identity.resolve = AsyncMock(return_value=ViewerIdentity(uid="42", nickname="viewer"))
    event = ViewerEvent(uid="42", nickname="viewer", source="live_danmaku", danmaku_text="关注了主播",
                        raw={"event_type": kind})
    viewer = await resolve_viewer_context(runtime, event, [], is_transient_event=False)
    assert viewer.profile.danmaku_count == 0
    assert not viewer.profile.last_interaction_summary
    _note_live_memory(runtime, {"status": "pushed", "event": event.to_dict()}, event=event)
    assert getattr(runtime, "live_memory", None) is None

    event.danmaku_text = "我喜欢小猫"
    event.raw["event_type"] = "danmaku"
    chat = await resolve_viewer_context(runtime, event, [], is_transient_event=False)
    assert chat.profile.danmaku_count == 1
    assert "关注了主播" not in chat.profile.last_interaction_summary
    assert "小猫" in chat.profile.last_interaction_summary
    runtime.dispatcher.live_target_lanlan = lambda: "CatA"
    _note_live_memory(runtime, {"status": "pushed", "event": event.to_dict()}, event=event)
    assert list(runtime.live_memory._buckets.values()) == [[{"role": "user", "content": "我喜欢小猫"}]]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["super_chat", "sc", "cheer"])
async def test_authored_paid_chat_still_learns_as_viewer_speech(runtime, kind):
    runtime.bili_identity.resolve = AsyncMock(return_value=ViewerIdentity(uid="42", nickname="viewer"))
    raw = {"event_type": kind}
    if kind == "cheer":
        raw = {"event_type": "gift", "support_verified": True,
               "support_evidence": "twitch_eventsub_typed_event", "provider_event_type": "TWITCH_CHEER"}
    event = ViewerEvent(uid="42", nickname="viewer", source="live_danmaku", danmaku_text="我喜欢小猫", raw=raw)
    viewer = await resolve_viewer_context(runtime, event, [], is_transient_event=False)
    assert viewer.profile.danmaku_count == 1
    runtime.dispatcher.live_target_lanlan = lambda: "CatA"
    _note_live_memory(runtime, {"status": "pushed", "event": event.to_dict()}, event=event)
    assert list(runtime.live_memory._buckets.values()) == [[{"role": "user", "content": "我喜欢小猫"}]]


def test_missing_sc_body_fallback_is_not_authored_speech():
    event = ViewerEvent(uid="42", source="live_danmaku", danmaku_text="Super Chat", raw={"event_type": "super_chat"})
    assert viewer_utterance_text(event) == ""


def test_batch_welcome_memory_uses_original_utterance_and_not_welcome_intent():
    runtime = memory_runtime()
    event = ViewerEvent(uid="twitch:42", source="live_danmaku", danmaku_text="直播间热闹起来了，需要欢迎大家",
                        raw={"event_type": "danmaku", "live_batch_welcome": "new_viewer_burst",
                             "original_danmaku_text": "今天想听小猫唱歌"})
    assert viewer_utterance_text(event) == "今天想听小猫唱歌"
    _note_live_memory(runtime, {"status": "pushed", "event": event.to_dict()}, event=event)
    assert list(runtime.live_memory._buckets.values()) == [[{"role": "user", "content": "今天想听小猫唱歌"}]]


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [
    {"status": "processed", "trust": {"persisted": False}},
    {"status": "processed", "trust": {"persisted": 1}},
    {"status": "processed", "trust": {}}, {"status": "processed"}, {}, [],
    {"status": "ignored", "trust": {"persisted": True}}, "invalid_json",
])
async def test_memory_unconfirmed_response_retains_batch_for_same_key_retry(monkeypatch, ack):
    from utils import internal_http_client

    bridge = LiveMemoryBridge(memory_runtime())
    bridge._memory_port = AsyncMock(return_value=1)
    requests = []
    responses = [ack, {"status": "processed", "trust": {"persisted": True}}]

    async def post(_url, *, json, **_kwargs):
        requests.append(json)
        response = responses.pop(0)

        def response_json():
            if response == "invalid_json":
                raise ValueError("private-response-body")
            return response

        return SimpleNamespace(raise_for_status=lambda: None, json=response_json)

    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(post=post, get=epochs_get()))
    bridge.note_exchange(uid="42", nickname="viewer", user_text="hello")
    await bridge.flush()
    assert len(bridge._buckets) == 1 and not bridge._posted_keys
    await bridge.flush()
    assert not bridge._buckets and len(bridge._posted_keys) == 1
    assert len(requests) == 2 and requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [True, None])
async def test_memory_confirmed_response_is_not_reposted(monkeypatch, persisted):
    from utils import internal_http_client

    bridge = LiveMemoryBridge(memory_runtime())
    bridge._memory_port = AsyncMock(return_value=1)
    post = AsyncMock(return_value=SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"status": "processed", "trust": {"persisted": persisted}},
    ))
    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(post=post, get=epochs_get()))
    bridge.note_exchange(uid="42", nickname="viewer", user_text="hello")
    await bridge.flush()
    await bridge.flush()
    post.assert_awaited_once()
    assert not bridge._buckets and len(bridge._posted_keys) == 1


def ok_post(requests, status=200):
    async def post(_url, *, json, **_kwargs):
        requests.append(json)
        return SimpleNamespace(
            status_code=status, raise_for_status=lambda: None,
            json=lambda: {"status": "processed", "trust": {"persisted": True}},
        )

    return post


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, expected", [({"epochs": {}}, 0), ({"epochs": {"participant:twitch:42": 3}}, 3)])
async def test_memory_keyed_post_carries_server_forget_epoch(monkeypatch, answer, expected):
    from utils import internal_http_client

    bridge = LiveMemoryBridge(memory_runtime())
    bridge._memory_port = AsyncMock(return_value=1)
    requests, get = [], epochs_get(answer)
    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(post=ok_post(requests), get=get))
    bridge.note_exchange(uid="42", nickname="viewer", user_text="hello")
    await bridge.flush()
    assert get.calls == [("http://127.0.0.1:1/internal/memory/A/forget_epochs", [("subject", "participant:twitch:42")])]
    assert requests[0]["subject_epochs"] == {"participant:twitch:42": expected}
    assert "scope" not in requests[0]["subject"]
    assert not bridge._buckets and not bridge._epoch_cache


@pytest.mark.asyncio
async def test_memory_retry_reuses_epoch_captured_for_the_key(monkeypatch):
    from utils import internal_http_client

    bridge = LiveMemoryBridge(memory_runtime())
    bridge._memory_port = AsyncMock(return_value=1)
    requests = []
    acks = [{"status": "ignored"}, {"status": "processed", "trust": {"persisted": True}}]

    async def post(_url, *, json, **_kwargs):
        requests.append(json)
        ack = acks.pop(0)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: ack)

    get = epochs_get({"epochs": {}}, {"epochs": {"participant:twitch:42": 9}})
    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(post=post, get=get))
    bridge.note_exchange(uid="42", nickname="viewer", user_text="hello")
    await bridge.flush()
    await bridge.flush()
    assert len(get.calls) == 1
    assert requests[0] == requests[1]
    assert requests[1]["subject_epochs"] == {"participant:twitch:42": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [503, {"epochs": {"participant:twitch:42": -1}}, {"epochs": []}, {}])
async def test_memory_unreadable_forget_epoch_holds_batch_without_posting(monkeypatch, answer):
    from utils import internal_http_client

    runtime = memory_runtime()
    audit = []
    runtime.audit = SimpleNamespace(record=lambda kind, _msg, **kw: audit.append((kind, kw["detail"])))
    bridge = LiveMemoryBridge(runtime)
    bridge._memory_port = AsyncMock(return_value=1)
    requests = []
    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(post=ok_post(requests), get=epochs_get(answer)))
    bridge.note_exchange(uid="42", nickname="viewer", user_text="hello")
    await bridge.flush()
    assert not requests and len(bridge._buckets) == 1
    assert audit == [("live_memory_unavailable", {"reason": "forget_epochs_unavailable"})]


@pytest.mark.asyncio
async def test_memory_rejected_request_is_dropped_not_retried(monkeypatch):
    from utils import internal_http_client

    bridge = LiveMemoryBridge(memory_runtime())
    bridge._memory_port = AsyncMock(return_value=1)
    requests = []
    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(post=ok_post(requests, 422), get=epochs_get()))
    bridge.note_exchange(uid="42", nickname="viewer", user_text="hello")
    await bridge.flush()
    await bridge.flush()
    assert len(requests) == 1
    assert not bridge._buckets and not bridge._posted_keys and not bridge._epoch_cache
    assert bridge._unavailable_reason == "memory_request_rejected"
