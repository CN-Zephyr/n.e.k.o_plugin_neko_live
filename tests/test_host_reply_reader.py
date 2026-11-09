from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.adapters.host_reply_reader import (
    CACHE_TTL_SECONDS,
    MAX_EVENT_REFS,
    MAX_REPLY_CHARS,
    MAX_REQUEST_REFS,
    HostReplyReader,
    ReplyScope,
)

SCOPE = ReplyScope("TestCat", 1, 1)


class Clock:
    value = 100.0

    def __call__(self):
        return self.value

    def advance(self):
        self.value += 2.0


class Conversations:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def get(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(items=self.rows)


def reader_for(bus=None):
    bus = bus or Conversations()
    plugin = SimpleNamespace(ctx=SimpleNamespace(bus=SimpleNamespace(conversations=bus)))
    clock = Clock()
    return HostReplyReader(plugin, monotonic=clock), bus, clock


def row(event_id="event-1", request_ids=("request-1",), *, content="Actual host reply", stamp=1.0):
    return {
        "lanlan_name": "TestCat",
        "conversation_id": "shared-turn",
        "turn_type": "assistant_message",
        "timestamp": stamp,
        "content": content,
        "metadata": {
            "role": "cat",
            "event_id": event_id,
            "reply_provenance": {
                "version": 1,
                "complete": True,
                "sources": [
                    {"kind": "plugin", "plugin_id": "neko_live", "request_id": request_id}
                    for request_id in request_ids
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_requires_registered_submission_and_accepts_all_owned_sources():
    reader, bus, _clock = reader_for(Conversations([row(request_ids=("request-1", "request-2"))]))
    assert await reader.prepare(SCOPE) == []
    assert bus.calls == []
    reader.submitted(SCOPE, "request-1")
    reader.submitted(SCOPE, "request-2")
    assert await reader.prepare(SCOPE) == ["Actual host reply"]
    assert bus.calls == [{"max_count": 32, "timeout": 0.2}]


@pytest.mark.asyncio
async def test_accepts_owned_proactive_reply_and_keeps_only_compact_text():
    observation = row(content="a" * 40)
    observation["turn_type"] = "proactive_reply"
    reader, _bus, _clock = reader_for(Conversations([observation]))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == ["a" * 24 + "..."]
    assert "a" * 40 not in repr(reader._replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["\r", "\r\n", "\x85", "\u2028", "\u2029"])
async def test_host_reply_is_cached_as_one_line(separator):
    reader, _bus, _clock = reader_for(Conversations([row(content=f"first{separator}second")]))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == ["first second"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(lanlan_name="OtherCat"),
        lambda r: r.update(turn_type="user_message"),
        lambda r: r.update(turn_type=[]),
        lambda r: r.pop("turn_type"),
        lambda r: r["metadata"].update(role="user"),
        lambda r: r["metadata"].pop("reply_provenance"),
        lambda r: r["metadata"].pop("event_id"),
        lambda r: r["metadata"].update(event_id="x" * 129),
        lambda r: r["metadata"]["reply_provenance"].update(version=True),
        lambda r: r["metadata"]["reply_provenance"].update(version=2),
        lambda r: r["metadata"]["reply_provenance"].update(complete=False),
        lambda r: r["metadata"]["reply_provenance"].update(complete=1),
        lambda r: r["metadata"]["reply_provenance"].update(sources=[]),
        lambda r: r["metadata"]["reply_provenance"]["sources"].append({"kind": "user"}),
        lambda r: r["metadata"]["reply_provenance"]["sources"].append({"kind": "unknown"}),
        lambda r: r["metadata"]["reply_provenance"]["sources"][0].update(plugin_id="other_plugin"),
        lambda r: r["metadata"]["reply_provenance"]["sources"][0].update(request_id="unknown-request"),
        lambda r: r["metadata"]["reply_provenance"]["sources"][0].pop("request_id"),
        lambda r: r["metadata"]["reply_provenance"]["sources"].append({
            "kind": "plugin", "plugin_id": "neko_live", "request_id": "unknown-request",
        }),
        lambda r: r["metadata"]["reply_provenance"].update(sources=[{
            "kind": "plugin", "plugin_id": "neko_live", "request_id": "request-1",
        }] * 17),
        lambda r: r.update(content=None),
        lambda r: r.update(content=" \n "),
    ],
)
async def test_rejects_unproven_or_mixed_ownership(change):
    observation = row()
    change(observation)
    reader, _bus, _clock = reader_for(Conversations([observation]))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == []


@pytest.mark.asyncio
async def test_uses_event_id_not_conversation_id_and_bounds_reply_window():
    observations = [row(f"event-{i}", content=f"reply-{i}", stamp=i) for i in reversed(range(8))]
    observations += [row("event-7", content="duplicate must not replace reply", stamp=7)]
    reader, bus, clock = reader_for(Conversations(observations))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == [f"reply-{i}" for i in range(2, 8)]
    clock.advance()
    assert await reader.prepare(SCOPE) == [f"reply-{i}" for i in range(2, 8)]
    assert len(bus.calls) == 2


@pytest.mark.asyncio
async def test_pending_seen_and_text_storage_are_bounded():
    reader, bus, clock = reader_for()
    for i in range(MAX_REQUEST_REFS + 1):
        reader.submitted(SCOPE, f"request-{i}")
    assert len(reader._pending) == MAX_REQUEST_REFS
    bus.rows = [row("unowned", request_ids=("request-0",))]
    assert await reader.prepare(SCOPE) == []
    for batch in range(5):
        clock.advance()
        bus.rows = [
            row(f"event-{batch}-{i}", content="x" * (MAX_REPLY_CHARS + 20), stamp=batch * 32 + i)
            for i in range(32)
        ]
        values = await reader.prepare(SCOPE)
    assert len(values) == 6
    assert all(len(value) <= MAX_REPLY_CHARS + 3 for value in values)
    assert len(reader._seen) == MAX_EVENT_REFS


@pytest.mark.asyncio
async def test_queries_at_most_once_per_two_seconds_and_returns_copy():
    reader, bus, clock = reader_for(Conversations([row()]))
    reader.submitted(SCOPE, "request-1")
    first = await reader.prepare(SCOPE)
    first.append("not stored")
    clock.value += 1.99
    assert await reader.prepare(SCOPE) == ["Actual host reply"]
    assert len(bus.calls) == 1
    clock.value += 0.01
    await reader.prepare(SCOPE)
    assert len(bus.calls) == 2


@pytest.mark.asyncio
async def test_expired_text_and_request_refs_are_removed_without_timer():
    reader, bus, clock = reader_for(Conversations([row()]))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == ["Actual host reply"]
    clock.value += CACHE_TTL_SECONDS
    assert await reader.prepare(SCOPE) == []
    assert reader._pending == {}
    assert reader._values() == []
    assert len(bus.calls) == 1


@pytest.mark.asyncio
async def test_expired_cache_can_query_a_current_submitted_request():
    reader, bus, clock = reader_for(Conversations([row()]))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == ["Actual host reply"]
    clock.value += CACHE_TTL_SECONDS
    reader.submitted(SCOPE, "request-new")
    bus.rows = [row("event-new", ("request-new",), content="New reply", stamp=2)]
    assert await reader.prepare(SCOPE) == ["New reply"]
    assert len(bus.calls) == 2


@pytest.mark.asyncio
async def test_scope_reset_close_and_reopen_drop_old_references():
    reader, bus, clock = reader_for(Conversations([row()]))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == ["Actual host reply"]
    next_scope = ReplyScope("TestCat", 2, 2)
    reader.submitted(next_scope, "request-new")
    assert await reader.prepare(next_scope) == []
    assert len(bus.calls) == 1  # Switching scope does not reset the RPC budget.
    clock.advance()
    assert await reader.prepare(next_scope) == []
    reader.close()
    reader.submitted(next_scope, "request-1")
    assert await reader.prepare(next_scope) == []
    assert reader._pending == {}
    reader.reopen()
    reader.submitted(next_scope, "request-1")
    clock.advance()
    assert await reader.prepare(next_scope) == ["Actual host reply"]


@pytest.mark.asyncio
async def test_same_scope_singleflight_and_cancelled_waiter_does_not_cancel_peer():
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingBus(Conversations):
        async def get(self, **kwargs):
            self.calls.append(kwargs)
            entered.set()
            await release.wait()
            return [row()]

    reader, bus, _clock = reader_for(BlockingBus())
    reader.submitted(SCOPE, "request-1")
    first = asyncio.create_task(reader.prepare(SCOPE))
    await entered.wait()
    second = asyncio.create_task(reader.prepare(SCOPE))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == ["Actual host reply"]
    assert len(bus.calls) == 1
    assert reader._inflight is None


@pytest.mark.asyncio
async def test_reset_rejects_late_result_even_when_rpc_suppresses_cancel():
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class LateBus(Conversations):
        async def get(self, **kwargs):
            self.calls.append(kwargs)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return [row()]

    reader, _bus, _clock = reader_for(LateBus())
    reader.submitted(SCOPE, "request-1")
    waiting = asyncio.create_task(reader.prepare(SCOPE))
    await entered.wait()
    reader.reset()
    await cancelled.wait()
    release.set()
    assert await waiting == []
    assert reader._values() == []
    assert reader._pending == {}
    assert reader._seen == {}


@pytest.mark.asyncio
async def test_caller_cancellation_during_reset_is_not_swallowed():
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingBus(Conversations):
        async def get(self, **kwargs):
            entered.set()
            await release.wait()
            return [row()]

    reader, _bus, _clock = reader_for(BlockingBus())
    reader.submitted(SCOPE, "request-1")
    waiting = asyncio.create_task(reader.prepare(SCOPE))
    await entered.wait()
    reader.reset()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert reader._values() == []


@pytest.mark.asyncio
async def test_shared_rpc_cancellation_without_invalidation_is_propagated():
    entered = asyncio.Event()

    class BlockingBus(Conversations):
        async def get(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    reader, _bus, _clock = reader_for(BlockingBus())
    reader.submitted(SCOPE, "request-1")
    waiting = asyncio.create_task(reader.prepare(SCOPE))
    await entered.wait()
    reader._inflight.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


@pytest.mark.asyncio
async def test_registration_during_rpc_does_not_retroactively_prove_ownership():
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingBus(Conversations):
        async def get(self, **kwargs):
            entered.set()
            await release.wait()
            return [row(request_ids=("request-new",))]

    reader, _bus, _clock = reader_for(BlockingBus())
    reader.submitted(SCOPE, "request-1")
    waiting = asyncio.create_task(reader.prepare(SCOPE))
    await entered.wait()
    reader.submitted(SCOPE, "request-new")
    release.set()
    assert await waiting == []


@pytest.mark.asyncio
async def test_missing_api_failure_and_sync_results_degrade_without_logging():
    reader = HostReplyReader(SimpleNamespace(ctx=None))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == []

    class SyncBus:
        def get(self, **_kwargs):
            return [SimpleNamespace(**row())]

    reader, _bus, clock = reader_for(SyncBus())
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == ["Actual host reply"]

    def fail(**_kwargs):
        raise RuntimeError("private response body must not be logged")

    reader._plugin.ctx.bus.conversations.get = fail
    clock.advance()
    assert await reader.prepare(SCOPE) == ["Actual host reply"]


@pytest.mark.asyncio
async def test_async_rpc_timeout_keeps_live_path_available(monkeypatch):
    from plugin.plugins.neko_live.adapters import host_reply_reader

    monkeypatch.setattr(host_reply_reader, "QUERY_TIMEOUT_SECONDS", 0.001)
    cancelled = asyncio.Event()

    class SlowBus:
        async def get(self, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    reader, _bus, _clock = reader_for(SlowBus())
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == []
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_timestamp_fallback_and_malformed_rows_do_not_enter_cache():
    earlier, later = row("earlier", content="earlier"), row("later", content="later")
    earlier["timestamp"] = float("nan")
    earlier["metadata"]["ts"] = 1.0
    later["timestamp"] = None
    later["metadata"]["ts"] = 2.0
    reader, _bus, _clock = reader_for(Conversations([later, None, "bad", earlier]))
    reader.submitted(SCOPE, "request-1")
    assert await reader.prepare(SCOPE) == ["earlier", "later"]
