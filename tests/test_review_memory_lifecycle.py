from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.adapters.live_memory_bridge import LiveMemoryBridge, scoped_history_payload
from plugin.plugins.neko_live.core.contracts import LiveConfig
from plugin.plugins.neko_live.core.event_bus import EventBus
from plugin.plugins.neko_live.core.module_registry_lifecycle import teardown_all_modules
from plugin.plugins.neko_live.core.runtime_live_input import _note_live_memory


def memory_runtime():
    return SimpleNamespace(
        config=LiveConfig(), dispatcher=SimpleNamespace(live_target_lanlan=lambda: "A"),
        live_provider=SimpleNamespace(platform="twitch"), _live_session_generation=1,
    )


@pytest.mark.asyncio
async def test_memory_preserves_collection_scope_and_excludes_submission_receipt():
    runtime = memory_runtime()
    _note_live_memory(runtime, {"status": "pushed", "output": "queued_to_neko(...)", "event": {"uid": "twitch:7", "danmaku_text": "hello"}})
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
