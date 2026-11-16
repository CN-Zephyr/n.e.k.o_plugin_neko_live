from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.core import event_bus as bus_module
from plugin.plugins.neko_live.core.event_bus import EventBus


@pytest.mark.asyncio
async def test_owner_limit_spans_event_types_and_preserves_sync_delivery(monkeypatch):
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS", 4)
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS_PER_OWNER", 2)
    bus = EventBus()
    release = asyncio.Event()
    delivered = []

    async def slow(_event):
        await release.wait()

    for kind in ("danmaku", "gift"):
        bus.subscribe(kind, slow, owner="slow_module")
        bus.subscribe(kind, delivered.append, owner="sync_module")
    try:
        for index in range(6):
            bus.publish("danmaku" if index % 2 else "gift", index)
        assert len(bus._tasks) == 2
        assert delivered == list(range(6))
        assert bus.status()["dropped_capacity_count"] == 4
    finally:
        release.set()
        await bus.close()


@pytest.mark.asyncio
async def test_global_limit_bounds_many_owners_and_closes_rejected_coroutines(monkeypatch):
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS", 3)
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS_PER_OWNER", 2)
    bus = EventBus()
    release = asyncio.Event()
    coroutines = []

    async def slow(_event):
        await release.wait()

    def handler(event):
        coro = slow(event)
        coroutines.append(coro)
        return coro

    try:
        for index in range(12):
            bus.subscribe(str(index), handler, owner=str(index))
            bus.publish(str(index), index)
        assert len(bus._tasks) == 3
        assert len(bus._pending_by_owner) == 3
        assert bus.status()["dropped_capacity_count"] == 9
        assert all(coro.cr_frame is None for coro in coroutines[3:])
    finally:
        release.set()
        await bus.close()
    assert bus.status()["pending_tasks"] == 0
    assert bus._pending_by_owner == {}


@pytest.mark.asyncio
async def test_saturated_owner_does_not_block_other_owner_and_recovers(monkeypatch):
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS_PER_OWNER", 1)
    bus = EventBus()
    release = asyncio.Event()
    delivered = []

    async def slow(event):
        await release.wait()
        delivered.append(event)

    bus.subscribe("slow", slow, owner="slow")
    bus.subscribe("other", slow, owner="other")
    try:
        bus.publish("slow", "first")
        bus.publish("slow", "dropped")
        bus.publish("other", "unrelated")
        assert len(bus._tasks) == 2
        release.set()
        await asyncio.gather(*bus._tasks)
        assert delivered == ["first", "unrelated"]
        bus.publish("slow", "recovered")
        await asyncio.gather(*bus._tasks)
        assert delivered == ["first", "unrelated", "recovered"]
        assert bus._pending_by_owner == {}
    finally:
        release.set()
        await bus.close()


@pytest.mark.asyncio
async def test_cancellation_before_first_run_closes_inner_coroutine_and_releases_owner(monkeypatch):
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS_PER_OWNER", 1)
    bus = EventBus()
    coroutines = []

    async def handler_body():
        await asyncio.Event().wait()

    def handler(_event):
        coro = handler_body()
        coroutines.append(coro)
        return coro

    bus.subscribe("event", handler, owner="owner")
    bus.publish("event", None)
    tasks = tuple(bus._tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert coroutines[0].cr_frame is None
    assert bus.status()["pending_tasks"] == 0
    assert bus._pending_by_owner == {}
    bus.publish("event", None)
    assert len(bus._tasks) == 1
    await bus.close(timeout=0)
    assert all(coro.cr_frame is None for coro in coroutines)
    assert bus._pending_by_owner == {}


@pytest.mark.asyncio
async def test_failed_handler_releases_its_capacity_and_logs_only_error_type(monkeypatch):
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS_PER_OWNER", 1)
    records = []
    bus = EventBus(SimpleNamespace(record=lambda *args, **kwargs: records.append((args, kwargs))))

    async def fail(_event):
        raise ValueError("private viewer text")

    bus.subscribe("event", fail, owner="owner")
    for _ in range(3):
        bus.publish("event", None)
        await asyncio.gather(*bus._tasks)
    assert len(records) == 3
    assert "private viewer text" not in str(records)
    assert bus.status()["dropped_capacity_count"] == 0
    assert bus._pending_by_owner == {}
    await bus.close()


def test_publish_without_running_loop_closes_coroutine_and_keeps_sync_subscribers():
    bus = EventBus()
    coroutines = []
    delivered = []

    async def handler_body():
        return

    def handler(_event):
        coro = handler_body()
        coroutines.append(coro)
        return coro

    bus.subscribe("event", handler, owner="async")
    bus.subscribe("event", delivered.append, owner="sync")
    bus.publish("event", 42)
    assert delivered == [42]
    assert coroutines[0].cr_frame is None
    assert bus.status()["pending_tasks"] == 0
    assert bus._pending_by_owner == {}


@pytest.mark.asyncio
async def test_reopening_does_not_release_capacity_while_cancelled_handler_finishes(monkeypatch):
    monkeypatch.setattr(bus_module, "_MAX_PENDING_TASKS_PER_OWNER", 1)
    bus = EventBus()
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    bus.subscribe("event", handler, owner="owner")
    bus.publish("event", None)
    await asyncio.wait_for(started.wait(), timeout=1)
    closing = asyncio.create_task(bus.close(timeout=0))
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        bus.reopen()
        bus.publish("event", None)
        assert bus.status()["pending_tasks"] == 1
        assert bus.status()["dropped_capacity_count"] == 1
    finally:
        release.set()
        await asyncio.wait_for(closing, timeout=1)
    assert bus.status()["pending_tasks"] == 0
    assert bus._pending_by_owner == {}
