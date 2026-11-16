from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.neko_live.core.contracts import LiveConfig, ViewerEvent, ViewerIdentity
from plugin.plugins.neko_live.core.safety_guard import SafetyGuard
from plugin.plugins.neko_live.modules.bili_live_ingest import BiliLiveIngestModule
from plugin.plugins.neko_live.modules.live_support_events import LiveSupportEventsModule


def _guard(limit=1):
    return SafetyGuard(LiveConfig(queue_limit=limit), SimpleNamespace(record=lambda *args, **kwargs: None))


def _event(uid="ordinary", event_type="danmaku"):
    return ViewerEvent(uid=uid, source="live_danmaku", raw={"event_type": event_type})


async def _occupy(guard, release, started):
    assert guard.before_event(_event()).allowed
    started.set()
    try:
        await release.wait()
    finally:
        guard.after_event()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 5])
@pytest.mark.parametrize("event_type", ["gift", "super_chat", "guard"])
async def test_support_waits_for_pipeline_capacity_without_losing_or_replaying_event(limit, event_type):
    guard = _guard(limit)
    release = asyncio.Event()
    starts = [asyncio.Event() for _ in range(limit)]
    active = [asyncio.create_task(_occupy(guard, release, started)) for started in starts]
    await asyncio.gather(*(started.wait() for started in starts))
    results = []

    async def handle(payload):
        decision = guard.before_event(_event(payload["uid"], payload["event_type"]))
        results.append(decision)
        if not decision.allowed:
            return {"status": "skipped"}
        try:
            assert guard.queue_size <= limit
            return {"status": "pushed"}
        finally:
            guard.after_event()

    module = LiveSupportEventsModule()
    await module.setup(SimpleNamespace(config=guard.config, audit=guard.audit, safety_guard=guard, handle_live_payload=handle))
    scheduler = module._scheduler
    try:
        assert scheduler.submit({"uid": "support", "event_type": event_type, "support_verified": True})
        await asyncio.sleep(0)
        assert results == []
        assert guard.queue_size == limit

        release.set()
        await asyncio.gather(*active)
        await asyncio.wait_for(scheduler.wait_idle(), timeout=1)

        assert len(results) == 1 and results[0].allowed
        assert guard.queue_size == 0
        assert guard.queue_overflows == 0
        assert scheduler.status()["pipeline_result_pushed_count"] == 1
    finally:
        release.set()
        await asyncio.gather(*active)
        await module.teardown()


@pytest.mark.asyncio
async def test_waiting_support_gets_next_slot_before_new_ordinary_event():
    guard = _guard()
    assert guard.before_event(_event()).allowed
    admitted = []

    async def support():
        async with guard.wait_for_capacity():
            decision = guard.before_event(_event("support", "super_chat"))
            admitted.append(decision.allowed)
            if decision.allowed:
                guard.after_event()

    task = asyncio.create_task(support())
    try:
        await asyncio.sleep(0)
        assert guard.snapshot()["waiting_for_capacity"] == 1
        guard.after_event()
        # A new event arrives before the awakened worker gets CPU time.
        assert not guard.before_event(_event("new-ordinary")).allowed
        await asyncio.wait_for(task, timeout=1)
        assert admitted == [True]
        assert guard.snapshot()["waiting_for_capacity"] == 0
        assert guard.before_event(_event("later-ordinary")).allowed
        guard.after_event()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "clear", "early_return", "exception"])
async def test_waiter_cleanup_does_not_leak_capacity_or_admission_priority(action):
    guard = _guard()
    assert guard.before_event(_event()).allowed

    async def support():
        async with guard.wait_for_capacity():
            if action == "exception":
                raise ValueError("pipeline rejected before admission")
            # Simulate a permission/session/input gate returning before admission.
            return

    task = asyncio.create_task(support())
    try:
        await asyncio.sleep(0)
        assert guard.snapshot()["waiting_for_capacity"] == 1
        if action == "cancel":
            task.cancel()
        elif action == "clear":
            guard.clear_queue()
        else:
            guard.after_event()
        outcome = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
        if action in {"cancel", "clear"}:
            assert isinstance(outcome[0], asyncio.CancelledError)
            assert guard.queue_size == 1
            guard.after_event()
        elif action == "exception":
            assert isinstance(outcome[0], ValueError)
        assert guard.snapshot()["waiting_for_capacity"] == 0
        assert guard.queue_size == 0
        assert guard.before_event(_event("fresh")).allowed
        guard.after_event()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["pause", "disconnect", "trip"])
async def test_changed_safety_gate_wakes_waiter_and_still_rejects_support(gate):
    guard = _guard()
    assert guard.before_event(_event()).allowed

    async def support():
        async with guard.wait_for_capacity():
            return guard.before_event(_event("support", "super_chat"))

    task = asyncio.create_task(support())
    try:
        await asyncio.sleep(0)
        if gate == "pause":
            guard.pause()
        elif gate == "disconnect":
            guard.set_connected(False)
        else:
            for _ in range(guard.config.safety_pipeline_failure_limit):
                guard.record_failure("pipeline", "synthetic failure")
        decision = await asyncio.wait_for(task, timeout=1)
        assert not decision.allowed
        assert decision.status == {"pause": "paused", "disconnect": "disconnected", "trip": "tripped"}[gate]
        assert guard.queue_size == 1
        assert guard.snapshot()["waiting_for_capacity"] == 0
    finally:
        guard.after_event()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_increased_capacity_wakes_waiter_without_waiting_for_active_work():
    guard = _guard()
    assert guard.before_event(_event()).allowed

    async def support():
        async with guard.wait_for_capacity():
            decision = guard.before_event(_event("support", "guard"))
            assert decision.allowed
            try:
                assert guard.queue_size == 2
            finally:
                guard.after_event()

    task = asyncio.create_task(support())
    try:
        await asyncio.sleep(0)
        guard.update(LiveConfig(queue_limit=2))
        await asyncio.wait_for(task, timeout=1)
        assert guard.queue_size == 1
        assert guard.snapshot()["waiting_for_capacity"] == 0
    finally:
        guard.after_event()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_waiting_support_runs_real_pipeline_and_rechecks_session(runtime, stale):
    runtime.config.live_enabled = True
    runtime.config.live_mode = "solo_stream"
    runtime.config.dry_run = True
    runtime.config.rate_limit_seconds = 0
    runtime.config.queue_limit = 1
    runtime._live_session_generation = 1
    runtime._accepting_live_events = True
    runtime.bili_live_ingest = BiliLiveIngestModule()
    runtime.bili_live_ingest.ctx = runtime
    runtime.live_status_summary = lambda _snapshot: {"can_output": True, "reason": "dry_run"}
    runtime.bili_identity.resolve = AsyncMock(return_value=ViewerIdentity(uid="42", nickname="viewer"))
    guard = runtime.safety_guard
    guard.set_connected(True)
    assert guard.before_event(_event()).allowed
    await runtime.live_support_events.setup(runtime)
    scheduler = runtime.live_support_events._scheduler
    try:
        assert scheduler.submit({
            "uid": "42", "nickname": "viewer", "event_type": "super_chat", "sc_price": 30,
            "support_verified": True, "support_evidence": "bilibili_typed_command",
            "_live_session_generation": 1,
        })
        await asyncio.sleep(0)
        runtime.bili_identity.resolve.assert_not_awaited()
        assert guard.snapshot()["waiting_for_capacity"] == 1
        if stale:
            runtime._live_session_generation = 2
        guard.after_event()
        await asyncio.wait_for(scheduler.wait_idle(), timeout=2)

        status = "skipped" if stale else "dry_run"
        assert scheduler.status()[f"pipeline_result_{status}_count"] == 1
        assert guard.queue_size == 0
        assert guard.snapshot()["waiting_for_capacity"] == 0
        assert not runtime.plugin.pushed_messages
        if not stale:
            result = runtime.recent_results[-1]
            assert result["status"] == "dry_run"
            steps = {step["id"] for step in result["steps"]}
            assert {"safety_guard.before_event", "safety_guard.before_output", "live_support_events", "neko_dispatcher"} <= steps
    finally:
        await runtime.live_support_events.teardown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["clear_pending", "reset", "teardown"])
async def test_support_lifecycle_cancels_capacity_wait_without_dispatch(action):
    guard = _guard()
    assert guard.before_event(_event()).allowed
    handle = AsyncMock()
    module = LiveSupportEventsModule()
    await module.setup(SimpleNamespace(config=guard.config, audit=guard.audit, safety_guard=guard, handle_live_payload=handle))
    scheduler = module._scheduler
    try:
        assert scheduler.submit({"uid": "support", "event_type": "guard", "support_verified": True})
        await asyncio.sleep(0)
        assert guard.snapshot()["waiting_for_capacity"] == 1
        if action == "teardown":
            await asyncio.wait_for(module.teardown(), timeout=1)
        else:
            getattr(module, action)()
        await asyncio.wait_for(scheduler.wait_idle(), timeout=1)
        handle.assert_not_awaited()
        assert guard.queue_size == 1
        assert guard.snapshot()["waiting_for_capacity"] == 0
        guard.after_event()
        assert guard.before_event(_event("fresh")).allowed
        guard.after_event()
    finally:
        await module.teardown()
