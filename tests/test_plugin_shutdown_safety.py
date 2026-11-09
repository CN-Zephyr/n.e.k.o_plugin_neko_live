from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.core.runtime import LiveRuntime
from plugin.plugins.neko_live.core.runtime_live_session import invalidate_live_session


@pytest.mark.asyncio
async def test_stop_removes_instruction_overlays_without_response_callbacks(
    runtime: LiveRuntime,
) -> None:
    runtime.instructions_injected = True
    runtime.instructions_signature = "active-scene"
    runtime.developer_instructions_injected = True

    await runtime.stop()

    assert len(runtime.plugin.pushed_messages) == 2
    assert {
        message["metadata"]["context_type"]
        for message in runtime.plugin.pushed_messages
    } == {"developer_mode", "live_scene"}
    assert all(
        message["metadata"]["delivery_intent"] == "passive_context"
        and message["metadata"]["context_expired"] is True
        and message["ai_behavior"] == "read"
        and message["visibility"] == []
        for message in runtime.plugin.pushed_messages
    )
    assert runtime.instructions_injected is False
    assert runtime.instructions_signature == ""
    assert runtime.developer_instructions_injected is False


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_context", ["live_scene", "developer_mode"])
async def test_stop_retries_rejected_restore_after_transport_can_progress(
    runtime: LiveRuntime,
    monkeypatch: pytest.MonkeyPatch,
    rejected_context: str,
) -> None:
    runtime.instructions_injected = True
    runtime.instructions_signature = "active-scene"
    runtime.developer_instructions_injected = True
    runtime.live_target_lanlan = "OriginalCat"
    transport_ready = asyncio.Event()
    attempts: list[dict] = []

    def push_message(**message):
        attempts.append(message)
        if message["metadata"]["context_type"] == rejected_context and not transport_ready.is_set():
            asyncio.get_running_loop().call_soon(transport_ready.set)
            return {"submitted": False, "reason": "backpressure"}
        return {"submitted": True}

    monkeypatch.setattr(runtime.plugin, "push_message", push_message)

    await runtime.stop()

    assert len(attempts) == 3
    assert sum(message["metadata"]["context_type"] == rejected_context for message in attempts) == 2
    assert all(message["metadata"]["context_expired"] is True for message in attempts)
    assert runtime.instructions_injected is False
    assert runtime.developer_instructions_injected is False
    assert runtime.live_target_lanlan == ""
    assert runtime._shutdown_complete is True
    stop_record = next(row for row in runtime.audit.recent() if row["op"] == "runtime_stop")
    assert stop_record["detail"]["failed_steps"] == []

    await runtime.stop()

    assert len(attempts) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rejected_context", "pending_flag", "failed_step"),
    [
        ("live_scene", "instructions_injected", "live_instructions"),
        ("developer_mode", "developer_instructions_injected", "developer_instructions"),
    ],
)
async def test_stop_retains_failed_restore_for_later_explicit_retry(
    runtime: LiveRuntime,
    monkeypatch: pytest.MonkeyPatch,
    rejected_context: str,
    pending_flag: str,
    failed_step: str,
) -> None:
    runtime.instructions_injected = True
    runtime.instructions_signature = "active-scene"
    runtime.developer_instructions_injected = True
    runtime.live_target_lanlan = "OriginalCat"
    reject = True
    attempts: list[dict] = []

    def push_message(**message):
        attempts.append(message)
        return {"submitted": not (reject and message["metadata"]["context_type"] == rejected_context)}

    monkeypatch.setattr(runtime.plugin, "push_message", push_message)

    await runtime.stop()

    assert len(attempts) == 3
    assert getattr(runtime, pending_flag) is True
    assert runtime._shutdown_complete is False
    stop_record = next(row for row in runtime.audit.recent() if row["op"] == "runtime_stop")
    assert stop_record["level"] == "warning"
    assert stop_record["detail"]["failed_steps"] == [failed_step]
    if rejected_context == "live_scene":
        assert runtime.live_target_lanlan == "OriginalCat"
        assert runtime.instructions_signature == "active-scene"
        assert runtime.developer_instructions_injected is False
    else:
        assert runtime.live_target_lanlan == ""
        assert runtime.instructions_injected is False

    reject = False
    await runtime.stop()

    assert len(attempts) == 4
    assert attempts[-1]["metadata"]["context_type"] == rejected_context
    if rejected_context == "live_scene":
        assert attempts[-1]["target_lanlan"] == "OriginalCat"
    assert runtime.instructions_injected is False
    assert runtime.developer_instructions_injected is False
    assert runtime.live_target_lanlan == ""
    assert runtime._shutdown_complete is True


@pytest.mark.asyncio
async def test_stop_timeout_cancels_restore_and_preserves_original_target(
    runtime: LiveRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.instructions_injected = True
    runtime.instructions_signature = "active-scene"
    runtime.live_target_lanlan = "OriginalCat"
    runtime._SHUTDOWN_STEP_TIMEOUT_SECONDS = 0.02
    cancelled = asyncio.Event()
    attempts: list[dict] = []

    async def push_message(**message):
        attempts.append(message)
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime.plugin, "push_message", push_message)

    await runtime.stop()

    assert cancelled.is_set()
    assert len(attempts) == 1
    assert runtime.instructions_injected is True
    assert runtime.instructions_signature == "active-scene"
    assert runtime.live_target_lanlan == "OriginalCat"
    assert runtime._shutdown_complete is False
    failure = next(row for row in runtime.audit.recent() if row["op"] == "runtime_stop_step_failed")
    assert failure["message"] == "shutdown step exceeded budget"
    assert failure["detail"]["step"] == "live_instructions"


def test_shutdown_invalidation_still_requests_replaceable_context_cleanup() -> None:
    reset_calls: list[dict[str, object]] = []
    runtime = SimpleNamespace(
        _live_session_generation=3,
        _stopping=True,
        live_audience_session=SimpleNamespace(finish_session=lambda: None),
        live_events=SimpleNamespace(reset=lambda **kwargs: reset_calls.append(kwargs)),
        live_support_events=SimpleNamespace(reset=lambda: None),
    )

    invalidate_live_session(runtime)

    assert reset_calls == [{}]
