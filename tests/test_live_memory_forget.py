from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.neko_live.adapters.live_memory_bridge import LiveMemoryBridge, MemoryScope
from plugin.plugins.neko_live.adapters.live_memory_forget import forget_viewer
from plugin.plugins.neko_live.core.contracts import LiveConfig
from plugin.plugins.neko_live.stores.memory_forget_store import MemoryForgetStore


def response(body, status=200):
    def raise_for_status():
        if status >= 300:
            raise RuntimeError("private response must never be surfaced")
    return SimpleNamespace(status_code=status, content=b"{}", json=lambda: body, raise_for_status=raise_for_status)


SUBJECT = {"subject_kind": "participant", "subject_id": "twitch:42", "scope": "participant:twitch:42"}
ACK = {"status": "forgotten", "subject": SUBJECT, "forgotten_subjects": [SUBJECT]}
SCHEMA = {"paths": {"/internal/memory/{lanlan_name}/scoped_forget": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Forget"}}}}}}}, "components": {"schemas": {"Forget": {"properties": {"subject": {}, "forget_epoch": {}, "target_epochs": {}}}}}}


@pytest.fixture
def memory(monkeypatch, tmp_path):
    from utils import internal_http_client

    runtime = SimpleNamespace(
        plugin=SimpleNamespace(data_path=lambda: tmp_path),
        config=LiveConfig(live_platform="twitch", live_enabled=False), safety_guard=SimpleNamespace(connected=False),
        dispatcher=SimpleNamespace(live_target_lanlan=lambda: "Cat/A"), live_provider=SimpleNamespace(platform="twitch"),
        viewer_store=SimpleNamespace(deletion_scope=lambda: "a" * 64, delete_profile=AsyncMock(return_value={"applied": True, "deleted": True})),
        pipeline=SimpleNamespace(session=SimpleNamespace(clear_uid=Mock())), audit=Mock(), _live_session_generation=1,
    )
    bridge = LiveMemoryBridge(runtime)
    bridge._memory_port = AsyncMock(return_value=1)

    async def get(url, **_kwargs):
        return response(SCHEMA if url.endswith("/openapi.json") else {"epochs": {"participant:twitch:42": 4}})

    client = SimpleNamespace(get=AsyncMock(side_effect=get), post=AsyncMock(return_value=response(ACK)))
    monkeypatch.setattr(internal_http_client, "get_internal_http_client", lambda: client)
    return bridge, client


async def erase(bridge, **overrides):
    return await forget_viewer(bridge, **{"uid": "42", "platform": "twitch", "target": "Cat/A", **overrides})


def restart(bridge):
    runtime = SimpleNamespace(**vars(bridge.runtime))
    runtime.memory_forget_store = MemoryForgetStore(runtime.plugin, runtime.audit)
    restored = LiveMemoryBridge(runtime)
    restored._memory_port = AsyncMock(return_value=1)
    return restored


@pytest.mark.asyncio
async def test_lost_receipt_survives_restart_without_auto_retry_or_new_epoch(memory):
    bridge, client = memory
    client.post.side_effect = [TimeoutError(), response(ACK)]
    with pytest.raises(OSError):
        await erase(bridge)
    restored = restart(bridge)
    assert client.post.await_count == 1
    for uid in ("42", "twitch:42"):
        restored.note_exchange(uid=uid, nickname="viewer", user_text="blocked")
    assert not restored._buckets
    restored.note_exchange(uid="43", nickname="other", user_text="allowed")
    assert len(restored._buckets) == 1
    await erase(restored, uid="twitch:42")
    assert client.post.call_args_list[0].kwargs["json"] == client.post.call_args_list[1].kwargs["json"]
    assert sum(not call.args[0].endswith("openapi.json") for call in client.get.call_args_list) == 1
    restored.runtime.viewer_store.delete_profile.assert_awaited_once_with("42", expected_scope="a" * 64)
    assert not restart(restored)._forget_pending


@pytest.mark.asyncio
async def test_confirmed_host_erase_restarts_with_only_local_cleanup(memory):
    bridge, client = memory
    bridge.runtime.viewer_store.delete_profile.side_effect = [OSError(), {"applied": True, "deleted": True}]
    with pytest.raises(OSError, match="local_delete_failed"):
        await erase(bridge)
    restored = restart(bridge)
    client.get.reset_mock()
    await erase(restored)
    client.get.assert_not_awaited()
    client.post.assert_awaited_once()
    assert not restored._forget_pending


@pytest.mark.asyncio
async def test_journal_cleanup_retry_does_not_repeat_local_or_host_deletion(memory, monkeypatch):
    bridge, client = memory
    monkeypatch.setattr(bridge.forget_store, "remove", AsyncMock(side_effect=OSError()))
    with pytest.raises(OSError):
        await erase(bridge)
    restored = restart(bridge)
    assert restored.forget_store.status()["pending"][0]["phase"] == "local_deleted"
    await erase(restored)
    client.post.assert_awaited_once()
    restored.runtime.viewer_store.delete_profile.assert_awaited_once()
    assert not restart(restored)._forget_pending


@pytest.mark.asyncio
async def test_failed_journal_checkpoint_prevents_first_host_erase(memory, monkeypatch):
    bridge, client = memory
    monkeypatch.setattr(bridge.forget_store, "_write", Mock(side_effect=OSError("private path")))
    with pytest.raises(OSError, match="^host_memory.journal_unavailable$"):
        await erase(bridge)
    client.post.assert_not_awaited()
    bridge.runtime.viewer_store.delete_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["port", "profile"])
async def test_restored_request_rejects_changed_storage_scope_before_network(memory, changed):
    bridge, client = memory
    client.post.side_effect = TimeoutError()
    with pytest.raises(OSError):
        await erase(bridge)
    restored = restart(bridge)
    if changed == "port":
        restored._memory_port.return_value = 2
    else:
        restored.runtime.viewer_store.deletion_scope = lambda: "b" * 64
    client.get.reset_mock()
    client.post.reset_mock()
    with pytest.raises(ValueError, match="scope_changed"):
        await erase(restored)
    client.get.assert_not_awaited()
    client.post.assert_not_awaited()
    restored.runtime.viewer_store.delete_profile.assert_not_awaited()
    assert restored._forget_pending


@pytest.mark.asyncio
async def test_cancelled_host_request_remains_recoverable(memory):
    bridge, client = memory
    entered = asyncio.Event()

    async def post(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client.post.side_effect = post
    task = asyncio.create_task(erase(bridge))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not bridge._forget_active
    restored = restart(bridge)
    assert restored._forget_pending == {("Cat/A", "twitch", "twitch:42"): 5}
    client.post.side_effect = None
    await erase(restored)
    assert not restored._forget_pending


@pytest.mark.asyncio
async def test_erase_has_explicit_single_subject_fence_and_confirmed_local_cleanup(memory):
    bridge, client = memory
    bridge.note_exchange(uid="twitch:42", nickname="viewer", user_text="pending private fact")
    bridge.note_exchange(uid="42", nickname="viewer", user_text="other platform", platform="douyin")
    result = await erase(bridge)
    assert result["forgotten"] and result["local_deleted"]
    args, kwargs = client.post.call_args
    assert args[0] == "http://127.0.0.1:1/internal/memory/Cat%2FA/scoped_forget"
    assert kwargs["json"] == {"subject": {"subject_kind": "participant", "subject_id": "twitch:42"}, "forget_epoch": 5, "target_epochs": {"participant:twitch:42": 5}}
    assert kwargs["follow_redirects"] is False
    assert [scope.platform for scope in bridge._buckets] == ["douyin"]
    bridge.runtime.viewer_store.delete_profile.assert_awaited_once_with("42", expected_scope="a" * 64)
    bridge.runtime.pipeline.session.clear_uid.assert_called_once_with("42")
    assert not bridge._forget_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("change,arguments", [("live", {}), ("connected", {}), ("none", {"platform": "douyin"}), ("none", {"target": "Another"}), ("none", {"uid": "../42"}), ("none", {"uid": None})])
async def test_invalid_or_active_scope_never_reaches_network(memory, change, arguments):
    bridge, client = memory
    if change == "live":
        bridge.runtime.config.live_enabled = True
    if change == "connected":
        bridge.runtime.safety_guard.connected = True
    with pytest.raises(ValueError, match="host_memory"):
        await erase(bridge, **arguments)
    client.get.assert_not_awaited()
    client.post.assert_not_awaited()
    bridge.runtime.viewer_store.delete_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [response({}, 404), response({}, 409), response({}, 422), response({}, 503), response({"status": "forgotten"}), response({**ACK, "subject": {**SUBJECT, "subject_id": "twitch:43"}}), response({**ACK, "forgotten_subjects": [SUBJECT, {**SUBJECT, "subject_id": "twitch:43"}]})])
async def test_unconfirmed_or_expanded_receipt_never_deletes_local_profile(memory, reply):
    bridge, client = memory
    client.post.return_value = reply
    with pytest.raises(OSError, match="host_memory") as exc:
        await erase(bridge)
    assert "private" not in str(exc.value)
    bridge.runtime.viewer_store.delete_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_host_that_ignores_unknown_fields_is_rejected_before_post(memory):
    bridge, client = memory
    client.get.side_effect = None
    client.get.return_value = response({"paths": {}})
    with pytest.raises(OSError, match="unsupported"):
        await erase(bridge)
    client.post.assert_not_awaited()
    bridge.runtime.viewer_store.delete_profile.assert_not_awaited()
    assert not bridge._forget_pending


@pytest.mark.asyncio
async def test_rejected_retry_does_not_release_an_earlier_uncertain_fence(memory):
    bridge, client = memory
    client.post.side_effect = [TimeoutError(), response({}, 409)]
    for _ in range(2):
        with pytest.raises(OSError, match="host_memory"):
            await erase(bridge)
    assert bridge._forget_pending == {("Cat/A", "twitch", "twitch:42"): 5}
    bridge.runtime.viewer_store.delete_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "local", "local_error"])
async def test_uncertain_retry_pins_epoch_and_blocks_prefixed_uid_writes(memory, failure):
    bridge, client = memory
    if failure == "timeout":
        client.post.side_effect = [TimeoutError("private request"), response(ACK)]
    elif failure == "local":
        bridge.runtime.viewer_store.delete_profile.side_effect = [{"applied": False}, {"applied": True, "deleted": True}]
    else:
        bridge.runtime.viewer_store.delete_profile.side_effect = [OSError("private file path"), {"applied": True, "deleted": True}]
    with pytest.raises(OSError, match="host_memory"):
        await erase(bridge)
    bridge.note_exchange(uid="twitch:42", nickname="viewer", user_text="must not recreate")
    assert not bridge._buckets
    assert await bridge.post_viewer("twitch:42", "viewer", [{"role": "user", "content": "late"}], scope=MemoryScope("Cat/A", "twitch", 1, "twitch:42")) is False
    assert len(client.post.call_args_list) == 1
    await erase(bridge)
    if failure == "timeout":
        assert client.post.call_args_list[0].kwargs["json"] == client.post.call_args_list[1].kwargs["json"]
    else:
        assert len(client.post.call_args_list) == 1  # Confirmed host erasure is not replayed for a local retry.
    assert sum(not call.args[0].endswith("openapi.json") for call in client.get.call_args_list) == 1
    assert not bridge._forget_pending


@pytest.mark.asyncio
async def test_scope_change_during_erase_does_not_delete_new_scope_local_profile(memory):
    bridge, client = memory

    async def post(*_args, **_kwargs):
        bridge.runtime.dispatcher.live_target_lanlan = lambda: "Another"
        return response(ACK)

    client.post.side_effect = post
    with pytest.raises(ValueError, match="scope_changed"):
        await erase(bridge)
    bridge.runtime.viewer_store.delete_profile.assert_not_awaited()
    assert bridge._forget_pending


@pytest.mark.asyncio
async def test_late_epoch_read_cannot_recreate_erased_subject(memory):
    bridge, client = memory
    entered, resume = asyncio.Event(), asyncio.Event()
    reads = 0

    async def epoch(*_args):
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            await resume.wait()
            return 4
        return 4

    bridge._forget_epoch = epoch
    old = asyncio.create_task(bridge.post_viewer("42", "viewer", [{"role": "user", "content": "late"}], scope=MemoryScope("Cat/A", "twitch", 1, "42")))
    await entered.wait()
    await erase(bridge)
    resume.set()
    assert await old is False
    assert len(client.post.call_args_list) == 1
    assert client.post.call_args.args[0].endswith("scoped_forget")


@pytest.mark.asyncio
async def test_cancelled_erase_retains_epoch_and_waiting_subject(memory):
    bridge, client = memory
    entered = asyncio.Event()

    async def post(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    client.post.side_effect = post
    task = asyncio.create_task(erase(bridge))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge._forget_pending == {("Cat/A", "twitch", "twitch:42"): 5}
    bridge.runtime.viewer_store.delete_profile.assert_not_awaited()
