from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.neko_live.adapters.live_memory_bridge import LiveMemoryBridge
from plugin.plugins.neko_live.core.contracts import LiveConfig


def _bridge() -> LiveMemoryBridge:
    runtime = SimpleNamespace(
        config=LiveConfig(),
        dispatcher=SimpleNamespace(live_target_lanlan=lambda: "ReviewCat"),
        live_provider=SimpleNamespace(platform="twitch"),
        _live_session_generation=1,
    )
    bridge = LiveMemoryBridge(runtime)
    bridge._memory_port = AsyncMock(return_value=1)
    return bridge


def _response(body, *, status=200):
    def raise_for_status():
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")

    return SimpleNamespace(status_code=status, raise_for_status=raise_for_status, json=lambda: body)


_ACK = {"status": "processed", "trust": {"persisted": True}}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "unconfirmed", "server_error"])
async def test_retry_keeps_original_payload_and_epoch_while_new_chat_is_separate(monkeypatch, failure):
    from utils import internal_http_client

    bridge = _bridge()
    epoch = [0]
    requests = []
    gets = []

    async def get(_url, *, params, **_kwargs):
        gets.append(epoch[0])
        return _response({"epochs": {params[0][1]: epoch[0]}})

    async def post(_url, *, json, **_kwargs):
        requests.append(copy.deepcopy(json))
        if len(requests) == 1:
            if failure == "timeout":
                raise TimeoutError("response lost after submission")
            if failure == "server_error":
                return _response({}, status=503)
            return _response({"status": "processed", "trust": {"persisted": False}})
        return _response(_ACK)

    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(get=get, post=post))
    for i in range(6):
        bridge.note_exchange(uid="42", nickname="old name", user_text=f"old fact {i}")
    await bridge.flush()
    assert len(requests) == 1  # No immediate retry loop after failure.

    # A host-side clear and a new speaker tier must not alter the old request.
    epoch[0] = 9
    bridge.note_exchange(uid="42", nickname="new name", user_text="new fact", guard_level=1)
    await bridge.flush()

    assert len(requests) == 3
    assert requests[1] == requests[0]
    assert gets == [0, 9]
    assert json.loads(requests[2]["input_history"]) == [{"role": "user", "content": "new fact"}]
    assert requests[2]["idempotency_key"] != requests[0]["idempotency_key"]
    assert requests[2]["subject_epochs"] == {"participant:twitch:42": 9}
    assert requests[2]["speaker_label"] == "new name"
    assert requests[2]["speaker_tier"] == "trusted"
    assert not bridge._batches and not bridge._buckets and not bridge._tasks and not bridge._epoch_cache


@pytest.mark.asyncio
async def test_retry_and_fresh_chat_share_existing_message_budget():
    bridge = _bridge()
    retry_entered, release = asyncio.Event(), asyncio.Event()
    submissions = []

    async def post(_uid, _nickname, messages, **_kwargs):
        submissions.append(copy.deepcopy(messages))
        if len(submissions) == 1:
            return False
        if len(submissions) == 2:
            retry_entered.set()
            await release.wait()
        return True

    bridge.post_viewer = post
    for i in range(6):
        bridge.note_exchange(uid="42", nickname="viewer", user_text=f"old {i}")
    await bridge.flush()
    bridge.note_exchange(uid="42", nickname="viewer", user_text="new 0")
    await retry_entered.wait()
    for i in range(1, 20):
        bridge.note_exchange(uid="42", nickname="viewer", user_text=f"new {i}")
    assert sum(len(batch.messages) for batch in bridge._batches.values()) == 6
    assert sum(len(bucket) for bucket in bridge._buckets.values()) == 6
    assert len(bridge._tasks) == 1
    release.set()
    await bridge.flush()
    assert submissions[1] == submissions[0]
    assert submissions[2] == [{"role": "user", "content": f"new {i}"} for i in range(14, 20)]
    assert not bridge._batches and not bridge._buckets


@pytest.mark.asyncio
async def test_chat_arriving_during_final_flush_round_resumes_without_another_event():
    bridge = _bridge()
    final_round_entered, release = asyncio.Event(), asyncio.Event()
    submissions = []

    async def post(_uid, _nickname, messages, **_kwargs):
        submissions.append(copy.deepcopy(messages))
        if len(submissions) == 1:
            return False
        if len(submissions) == 3:
            final_round_entered.set()
            await release.wait()
        return True

    bridge.post_viewer = post
    bridge.note_exchange(uid="42", nickname="viewer", user_text="retry fact")
    await bridge.flush()
    bridge.note_exchange(uid="42", nickname="viewer", user_text="fresh tail")
    flushing = asyncio.create_task(bridge.flush())
    await final_round_entered.wait()
    for i in range(6):
        bridge.note_exchange(uid="42", nickname="viewer", user_text=f"later {i}")
    release.set()
    await flushing
    await asyncio.gather(*list(bridge._tasks))
    assert len(submissions) == 4
    assert submissions[-1] == [{"role": "user", "content": f"later {i}"} for i in range(6)]
    assert not bridge._tasks and not bridge._batches and not bridge._buckets


@pytest.mark.asyncio
async def test_failed_batches_share_existing_scope_budget_and_release_epochs_on_eviction(monkeypatch):
    from utils import internal_http_client

    bridge = _bridge()
    get = AsyncMock(return_value=_response({"epochs": {}}))
    post = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: SimpleNamespace(get=get, post=post))
    for uid in range(256):
        bridge.note_exchange(uid=str(uid), nickname="viewer", user_text="pending fact")
    await bridge.flush()
    assert len(bridge._batches) == len(bridge._buckets) == len(bridge._epoch_cache) == 256
    assert post.await_count == 256
    bridge.note_exchange(uid="replacement", nickname="viewer", user_text="new scope")
    assert len(bridge._buckets) == 256
    assert len(bridge._batches) == len(bridge._epoch_cache) == 255
    assert all(scope.uid != "0" for scope in bridge._batches)
    bridge.discard()
    assert not bridge._batches and not bridge._buckets and not bridge._epoch_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["discard_uid", "disable", "close"])
async def test_retry_cleanup_cancels_owned_request_and_drops_both_message_sets(operation):
    bridge = _bridge()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bridge.post_viewer = post
    bridge.note_exchange(uid="42", nickname="viewer", user_text="old fact")
    await bridge.flush()
    bridge.note_exchange(uid="42", nickname="viewer", user_text="new fact")
    await entered.wait()
    for i in range(5):
        bridge.note_exchange(uid="42", nickname="viewer", user_text=f"buffered fresh fact {i}")
    if operation == "discard_uid":
        bridge.discard("42")
        await bridge.flush()
    elif operation == "disable":
        bridge.runtime.config.viewer_memory_enabled = False
        await bridge.flush()
    else:
        closing = asyncio.create_task(bridge.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    assert cancelled.is_set()
    assert calls == 2
    assert not bridge._tasks and not bridge._batches and not bridge._buckets and not bridge._metadata
