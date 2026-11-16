from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from plugin.plugins.neko_live.core.contracts import ViewerIdentity
from plugin.plugins.neko_live.stores import memory_forget_store as journal
from plugin.plugins.neko_live.stores.memory_forget_store import ForgetRequest, MemoryForgetStore
from plugin.plugins.neko_live.stores.viewer_store import ViewerStore


def make_store(path):
    return MemoryForgetStore(SimpleNamespace(data_path=lambda: path), Mock())


def request(uid="42"):
    return ForgetRequest("Cat", "twitch", uid, 5, 48912, "a" * 64)


@pytest.mark.asyncio
async def test_restart_restores_exact_scope_epoch_and_phase_without_mutation(tmp_path):
    store = make_store(tmp_path)
    original = request()
    assert not (tmp_path / "memory_forget_pending.json").exists()
    saved, created = await store.reserve(original)
    assert created and saved == original
    restored = make_store(tmp_path)
    assert restored.available
    assert restored.blocks(("Cat", "twitch", "twitch:42"))
    assert not restored.blocks(("Other", "twitch", "twitch:42"))
    assert restored.entries[original.owner] == original
    reused, created = await restored.reserve(replace(original, epoch=8))
    assert not created and reused.epoch == 5
    confirmed = await restored.checkpoint(reused, "host_confirmed")
    assert make_store(tmp_path).entries[original.owner] == confirmed
    await restored.remove(confirmed)
    assert make_store(tmp_path).entries == {}
    assert json.loads(store.path.read_bytes()) == {"version": 1, "requests": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"{", b"[]", b'{"version":2,"requests":[]}', b'{"version":true,"requests":[]}', b" " * (journal.MAX_BYTES + 1)], ids=["malformed", "list", "version", "bool-version", "oversized"])
async def test_corrupt_or_oversized_journal_is_preserved_and_blocks_memory(tmp_path, raw):
    path = tmp_path / "memory_forget_pending.json"
    path.write_bytes(raw)
    store = make_store(tmp_path)
    assert not store.available and store.blocks(request().owner)
    assert store.blocks_profile("unrelated")
    with pytest.raises(OSError, match="journal_unavailable"):
        await store.reserve(request())
    assert path.read_bytes() == raw
    assert store.audit.record.call_count == 1


@pytest.mark.asyncio
async def test_capacity_is_bounded_and_no_active_request_is_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "MAX_REQUESTS", 2)
    store = make_store(tmp_path)
    await store.reserve(request("1"))
    await store.reserve(request("2"))
    before = store.path.read_bytes()
    with pytest.raises(OSError):
        await store.reserve(request("3"))
    assert store.path.read_bytes() == before
    restored = make_store(tmp_path)
    assert {row.uid for row in restored.entries.values()} == {"1", "2"}


@pytest.mark.asyncio
async def test_failed_replace_preserves_old_journal_and_recovery_state(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    original, _ = await store.reserve(request())
    before = store.path.read_bytes()
    original_replace = journal.os.replace
    monkeypatch.setattr(journal.os, "replace", Mock(side_effect=OSError("private path")))
    with pytest.raises(OSError, match="^host_memory.journal_unavailable$"):
        await store.checkpoint(original, "host_confirmed")
    assert not store.available
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob(".memory_forget-*.tmp"))
    monkeypatch.setattr(journal.os, "replace", original_replace)
    await store.reload()
    assert store.available and store.entries[original.owner].phase == "prepared"


@pytest.mark.asyncio
async def test_cancelled_disk_commit_finishes_before_lock_release_and_updates_cache(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    entered, released = threading.Event(), threading.Event()
    write = store._write

    def slow(entries):
        entered.set()
        if not released.wait(3):
            raise TimeoutError("test release missing")
        write(entries)

    monkeypatch.setattr(store, "_write", slow)
    task = asyncio.create_task(store.reserve(request()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.entries[request().owner] == request()
    assert make_store(tmp_path).entries == store.entries


@pytest.mark.asyncio
async def test_separate_store_instances_merge_and_reuse_first_reserved_request(tmp_path):
    first, second = make_store(tmp_path), make_store(tmp_path)
    await asyncio.gather(first.reserve(request("1")), second.reserve(request("2")))
    assert len(make_store(tmp_path).entries) == 2
    results = await asyncio.gather(first.reserve(request()), second.reserve(replace(request(), epoch=6)))
    assert sum(created for _, created in results) == 1
    assert results[0][0] == results[1][0]


@pytest.mark.parametrize("changes", [{"epoch": True}, {"epoch": 0}, {"port": -1}, {"uid": "../bad"}, {"profile_scope": "private/path"}, {"phase": "completed"}])
def test_invalid_record_is_not_partially_restored(tmp_path, changes):
    data = asdict(request()) | changes
    file = tmp_path / "memory_forget_pending.json"
    file.write_text(json.dumps({"version": 1, "requests": [asdict(request("1")), data]}), encoding="utf-8")
    store = make_store(tmp_path)
    assert not store.available and not store.entries


@pytest.mark.asyncio
async def test_stale_cleanup_cannot_remove_an_advanced_checkpoint(tmp_path):
    store = make_store(tmp_path)
    original, _ = await store.reserve(request())
    confirmed = await store.checkpoint(original, "host_confirmed")
    with pytest.raises(OSError):
        await store.remove(original)
    restored = make_store(tmp_path)
    assert restored.entries[original.owner] == confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_restored_journal_blocks_local_recreation_until_cleanup(tmp_path, unavailable):
    original = make_store(tmp_path)
    await original.reserve(request())
    if unavailable:
        original.path.write_bytes(b"corrupt")
    restored = make_store(tmp_path)
    profiles = ViewerStore(SimpleNamespace(data_path=lambda: tmp_path), audit=None, write_blocked_provider=restored.blocks_profile)
    for uid in ("42", "twitch:42"):
        identity = ViewerIdentity(uid=uid, nickname="synthetic")
        await profiles.upsert_identity(identity)
        await profiles.record_live_danmaku(identity, "synthetic")
        assert not await profiles.mark_roasted(uid, "synthetic")
    assert not await profiles.recent_profiles()
    await profiles.upsert_identity(ViewerIdentity(uid="43", nickname="other"))
    assert len(await profiles.recent_profiles()) == (0 if unavailable else 1)
    if not unavailable:
        await restored.remove(request())
        await profiles.upsert_identity(ViewerIdentity(uid="42", nickname="new interaction"))
        assert len(await profiles.recent_profiles()) == 2


@pytest.mark.asyncio
async def test_local_delete_revalidates_profile_directory_inside_write_lock(tmp_path):
    location = [str(tmp_path / "first")]
    plugin = SimpleNamespace(data_path=lambda: tmp_path)
    profiles = ViewerStore(plugin, audit=None, dir_provider=lambda: location[0])
    await profiles.upsert_identity(ViewerIdentity(uid="42", nickname="first"))
    scope = profiles.deletion_scope()
    async with profiles._lock:
        task = asyncio.create_task(profiles.delete_profile("42", expected_scope=scope))
        await asyncio.sleep(0)
        assert not task.done()
        location[0] = str(tmp_path / "second")
        other = ViewerStore(plugin, audit=None, dir_provider=lambda: location[0])
        await other.upsert_identity(ViewerIdentity(uid="42", nickname="second"))
    with pytest.raises(ValueError, match="profile_scope_changed"):
        await task
    assert (await profiles.recent_profiles())[0]["nickname"] == "second"
    location[0] = str(tmp_path / "first")
    assert (await profiles.recent_profiles())[0]["nickname"] == "first"
