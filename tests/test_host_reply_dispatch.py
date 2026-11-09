from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.core import live_hosting_gates, runtime_config
from plugin.plugins.neko_live.core.contracts import InteractionRequest, ViewerEvent, ViewerIdentity, ViewerProfile
from plugin.plugins.neko_live.core.pipeline_dispatch import dispatch_routed_request
from plugin.plugins.neko_live.core.runtime_live_session import invalidate_live_session


def request_for(runtime, *, source="live_danmaku", dry_run=False):
    return InteractionRequest(
        event=ViewerEvent(
            uid="viewer", nickname="viewer", source=source, danmaku_text="hello",
            raw={"_live_session_generation": runtime._live_session_generation},
        ),
        identity=ViewerIdentity(uid="viewer", nickname="viewer"),
        profile=ViewerProfile(uid="viewer", nickname="viewer"),
        prompt_text="Respond to the current viewer.", live_mode="solo_stream",
        strength="normal", dry_run=dry_run,
    )


def activate(runtime):
    runtime._accepting_live_events = True
    runtime._live_session_generation = 1
    runtime._live_listener_operation_id = 1
    runtime.live_target_lanlan = "Cat"
    runtime.live_connection_state = "connected"
    runtime.safety_guard.set_connected(True)
    runtime.config.dry_run = False
    runtime.config.rate_limit_seconds = 0
    runtime.plugin.runtime = runtime


def host_record(request_id, text="Actual generated reply.", *, event_id="reply-event"):
    return SimpleNamespace(
        lanlan_name="Cat", content=text, timestamp=10.0, turn_type="assistant_message",
        metadata={
            "role": "cat", "event_id": event_id,
            "reply_provenance": {
                "version": 1, "complete": True,
                "sources": [{"kind": "plugin", "plugin_id": "neko_live", "request_id": request_id}],
            },
        },
    )


async def route(runtime, request):
    return await dispatch_routed_request(
        runtime, SimpleNamespace(), event=request.event, identity=request.identity,
        profile=request.profile, request=request, steps=[], response_module_id="danmaku_response",
        should_mark_roasted=False, mark_avatar_roast_sent=lambda: None,
    )


@pytest.mark.asyncio
async def test_observed_reply_only_enters_local_delivery_prompt(runtime, monkeypatch):
    activate(runtime)
    rows = []
    calls = []

    async def get(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(items=rows)

    runtime.plugin.ctx = SimpleNamespace(bus=SimpleNamespace(conversations=SimpleNamespace(get=get)))
    first = request_for(runtime)
    prepared = await runtime.dispatcher.prepare_recent_replies(first)
    assert calls == []  # There is no owned submitted request to query yet.
    await runtime.dispatcher.push_roast(first, prepared_replies=prepared)
    request_id = runtime.plugin.pushed_messages[-1]["metadata"]["task_id"]
    assert len(request_id) == 32 and int(request_id, 16) >= 0
    rows.append(host_record(request_id, "ACTUAL_PRIVATE_REPLY_SENTINEL"))
    second = request_for(runtime)
    original_prompt = second.prompt_text
    saved = []
    monkeypatch.setattr("plugin.plugins.neko_live.core.runtime_live_input.save_recent_results", lambda rt: saved.append(list(rt.recent_results)))
    result = await route(runtime, second)
    assert result.status == "pushed"
    assert len(calls) == 1
    assert calls[0]["max_count"] == 32 and calls[0]["timeout"] == 0.2
    payload = runtime.plugin.pushed_messages[-1]
    assert "ACTUAL_PRIVATE_REPLY_SEN" in payload["parts"][0]["text"]
    assert second.prompt_text == original_prompt
    assert "task_id" not in second.metadata
    public_state = json.dumps({"request": second.to_public_dict(), "results": list(runtime.recent_results), "audit": runtime.audit.recent(100), "saved": saved})
    assert "ACTUAL_PRIVATE_REPLY" not in public_state
    assert request_id not in public_state


@pytest.mark.asyncio
async def test_combined_negative_examples_keep_existing_six_item_budget(runtime):
    activate(runtime)
    runtime.recent_results.extend({"status": "pushed", "output": f"Local example {i}"} for i in range(6))
    prepared = await runtime.dispatcher.prepare_recent_replies(request_for(runtime))
    prepared = type(prepared)(prepared.scope, prepared.epoch, tuple(f"Host example {i} " + "x" * 40 for i in range(6)))
    await runtime.dispatcher.push_roast(request_for(runtime), prepared_replies=prepared)
    text = runtime.plugin.pushed_messages[-1]["parts"][0]["text"]
    assert "Local example" not in text
    assert text.count("Host example") == 6
    assert "x" * 25 not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["operation", "target", "session", "stop", "dry_run", "pause", "queue"])
async def test_query_wait_cannot_bypass_current_output_gates(runtime, change):
    activate(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(_scope):
        entered.set()
        await release.wait()
        return ["Late private response"]

    runtime.dispatcher._reply_reader.prepare = prepare
    request = request_for(runtime)
    before_output = runtime.safety_guard.before_output
    gate_calls = []

    def gate(*args, **kwargs):
        gate_calls.append(kwargs)
        return before_output(*args, **kwargs)

    runtime.safety_guard.before_output = gate

    async def run():
        runtime.safety_guard.before_event(request.event)
        try:
            return await route(runtime, request)
        finally:
            runtime.safety_guard.after_event()

    pending = asyncio.create_task(run())
    await entered.wait()
    assert gate_calls == []
    if change == "operation":
        runtime._live_listener_operation_id += 1
    elif change == "target":
        runtime.live_target_lanlan = "OtherCat"
    elif change == "session":
        runtime._live_session_generation += 1
    elif change == "stop":
        runtime._stopping = True
    elif change == "dry_run":
        runtime.config.dry_run = True
    elif change == "pause":
        runtime.safety_guard.pause()
    else:
        runtime.safety_guard.clear_queue()
    release.set()
    if change == "queue":
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        result = await pending
        assert result.status == "skipped"
    assert runtime.plugin.pushed_messages == []
    assert len(gate_calls) <= 1


@pytest.mark.asyncio
async def test_prepare_keeps_frozen_owner_and_consumes_cooldown_once(runtime):
    activate(runtime)
    runtime.plugin.ctx = SimpleNamespace(_current_lanlan="OtherCat")
    runtime.config.rate_limit_seconds = 20
    original = runtime.safety_guard.before_output
    calls = []

    def gate(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    runtime.safety_guard.before_output = gate
    result = await route(runtime, request_for(runtime))
    assert result.status == "pushed"
    assert calls == [{}]
    assert runtime.plugin.pushed_messages[-1]["target_lanlan"] == "Cat"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dry_run", "non_deliverable", "sandbox"])
async def test_non_live_delivery_does_not_read_or_register(runtime, mode):
    activate(runtime)

    async def forbidden(_scope):
        raise AssertionError("no host read permitted")

    runtime.dispatcher._reply_reader.prepare = forbidden
    request = request_for(runtime, dry_run=mode == "dry_run", source="developer_sandbox" if mode == "sandbox" else "live_danmaku")
    if mode == "non_deliverable":
        request.should_push = False
    assert await runtime.dispatcher.prepare_recent_replies(request) is None


@pytest.mark.asyncio
async def test_late_submission_cannot_register_in_replaced_session(runtime):
    activate(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    registrations = []

    async def push(**kwargs):
        entered.set()
        await release.wait()
        return {"submitted": True}

    runtime.plugin.push_message = push
    runtime.dispatcher._reply_reader.submitted = lambda *args: registrations.append(args)
    request = request_for(runtime)
    prepared = await runtime.dispatcher.prepare_recent_replies(request)
    pending = asyncio.create_task(runtime.dispatcher.push_roast(request, prepared_replies=prepared))
    await entered.wait()
    runtime.dispatcher.reset_reply_reader()
    runtime._live_session_generation += 1
    release.set()
    await pending
    assert registrations == []


@pytest.mark.asyncio
async def test_rejected_submission_is_not_registered(runtime):
    activate(runtime)
    registrations = []
    runtime.plugin.push_message = lambda **kwargs: {"submitted": False}
    runtime.dispatcher._reply_reader.submitted = lambda *args: registrations.append(args)
    request = request_for(runtime)
    prepared = await runtime.dispatcher.prepare_recent_replies(request)
    with pytest.raises(RuntimeError, match="submission_rejected"):
        await runtime.dispatcher.push_roast(request, prepared_replies=prepared)
    assert registrations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dry_run", "session"])
async def test_reader_reset_does_not_terminate_real_hosting_loop(runtime, monkeypatch, boundary):
    activate(runtime)
    runtime.config.live_mode = "solo_stream"
    runtime.config.live_enabled = True
    runtime.config.live_room_id = 123
    await runtime.bili_live_ingest.start_listening(123)
    runtime.live_room_context = {"platform": "bilibili", "room_ref": "123", "live_status": "live"}
    entered, next_tick = asyncio.Event(), asyncio.Event()
    sleeps = 0

    async def controlled_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            next_tick.set()
            await asyncio.Event().wait()

    async def get(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def noop(*args, **kwargs):
        return None

    runtime.plugin.ctx = SimpleNamespace(bus=SimpleNamespace(conversations=SimpleNamespace(get=get)))
    request = request_for(runtime)
    scope = runtime.dispatcher._reply_scope(request)
    runtime.dispatcher._reply_reader.submitted(scope, "previous-live-submission")
    monkeypatch.setattr(runtime, "_idle_hosting_sleep", controlled_sleep)
    monkeypatch.setattr(live_hosting_gates, "hosting_live_state", lambda _runtime: {
        "state": "warmup", "warmup_hosting_candidate": True,
    })
    monkeypatch.setattr(runtime_config, "reconcile_live_listener_after_config", noop)
    runtime._start_idle_hosting_loop()
    task = runtime._idle_hosting_task
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        if boundary == "dry_run":
            await runtime_config._commit_loaded_config(runtime, replace(runtime.config, dry_run=True))
        else:
            invalidate_live_session(runtime)
        # The real director -> pipeline -> reader path must return to its loop.
        await asyncio.wait_for(next_tick.wait(), 1.0)
        assert not task.done()
        assert runtime.plugin.pushed_messages == []
        assert not any(item["op"] == "idle_hosting_loop_failed" for item in runtime.audit.recent(20))
    finally:
        await runtime._stop_idle_hosting_loop()
