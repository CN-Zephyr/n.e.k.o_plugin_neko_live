from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from plugin.plugins.neko_live.core.contracts import ViewerIdentity
from plugin.plugins.neko_live.stores import viewer_store


def make_store(directory, **kwargs):
    audit = Mock()
    return viewer_store.ViewerStore(SimpleNamespace(data_path=lambda: directory), audit, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_enabled", [True, False])
async def test_full_store_preserves_existing_profiles_and_returns_transient_new_viewers(
    tmp_path, monkeypatch, memory_enabled,
):
    monkeypatch.setattr(viewer_store, "_MAX_PROFILES", 2)
    store = make_store(tmp_path, memory_enabled_provider=lambda: memory_enabled)
    await store.upsert_identity(ViewerIdentity(uid="1", nickname="one"))
    await store.upsert_identity(ViewerIdentity(uid="2", nickname="two"))
    file = tmp_path / "viewer_profiles.json"
    before = file.read_bytes()

    profile = await store.upsert_identity(ViewerIdentity(uid="3", nickname="temporary"))
    learned = await store.record_live_danmaku(ViewerIdentity(uid="4", nickname="temporary"), "喜欢猫猫")
    assert profile.uid == "3" and profile.nickname == "temporary"
    assert learned.uid == "4" and learned.danmaku_count == 1
    assert await store.mark_roasted("5", "result") is False
    assert file.read_bytes() == before
    assert [call.args for call in store.audit.record.call_args_list] == [
        ("viewer_store_capacity_reached", "profile_count"),
    ]
    assert store.storage_status()["capacity_reasons"] == ["profile_count"]

    await store.upsert_identity(ViewerIdentity(uid="1", nickname="updated"))
    await store.record_live_danmaku(ViewerIdentity(uid="2", nickname="two"), "hello")
    assert await store.mark_roasted("1", "done") is True
    persisted = json.loads(file.read_text(encoding="utf-8"))
    assert set(persisted) == {"1", "2"}
    assert persisted["1"]["nickname"] == "updated"
    assert persisted["1"]["roast_count"] == 1
    assert persisted["2"]["danmaku_count"] == 1
    assert (await store.delete_profile("2"))["applied"] is True
    assert store.storage_status()["capacity_reasons"] == []
    await store.upsert_identity(ViewerIdentity(uid="3", nickname="now persistent"))
    assert {item["uid"] for item in await make_store(tmp_path).recent_profiles()} == {"1", "3"}


@pytest.mark.asyncio
async def test_concurrent_admission_shares_one_limit_across_all_write_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_store, "_MAX_PROFILES", 3)
    store = make_store(tmp_path)
    await asyncio.gather(*[
        action
        for number in range(5)
        for action in (
            store.upsert_identity(ViewerIdentity(uid=f"identity-{number}", nickname="one")),
            store.record_live_danmaku(ViewerIdentity(uid=f"live-{number}", nickname="two"), "hi"),
            store.mark_roasted(f"roast-{number}", "done"),
        )
    ])
    assert len(json.loads((tmp_path / "viewer_profiles.json").read_text(encoding="utf-8"))) == 3


@pytest.mark.asyncio
async def test_expired_profile_frees_capacity_without_evicting_active_viewer(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_store, "_MAX_PROFILES", 2)
    file = tmp_path / "viewer_profiles.json"
    file.write_text(json.dumps({
        "old": {"uid": "old", "last_seen_at": "2000-01-01T00:00:00+00:00"},
        "active": {"uid": "active", "nickname": "existing"},
    }), encoding="utf-8")
    store = make_store(tmp_path)
    await store.upsert_identity(ViewerIdentity(uid="new", nickname="new"))
    assert set(json.loads(file.read_text(encoding="utf-8"))) == {"active", "new"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["file_size", "profile_count"])
async def test_oversized_legacy_store_is_preserved_and_never_quarantined(tmp_path, monkeypatch, reason):
    file = tmp_path / "viewer_profiles.json"
    raw = json.dumps({"1": {"uid": "1", "nickname": "existing"}, "2": {"uid": "2"}}).encode()
    file.write_bytes(raw)
    if reason == "file_size":
        monkeypatch.setattr(viewer_store, "_MAX_STORE_BYTES", len(raw) - 1)
    else:
        monkeypatch.setattr(viewer_store, "_MAX_PROFILES", 1)
    store = make_store(tmp_path)
    assert (await store.upsert_identity(ViewerIdentity(uid="new", nickname="new"))).uid == "new"
    with pytest.raises(viewer_store.ViewerStoreUnavailableError):
        await store.record_live_danmaku(ViewerIdentity(uid="new", nickname="new"), "hello")
    assert await store.mark_roasted("1", "done") is False
    assert (await store.clear_profiles())["applied"] is False
    assert (await store.delete_profile("1"))["applied"] is False
    assert (await store.prune_expired_profiles())["applied"] is False
    assert await store.recent_profiles() == []
    assert file.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [file]
    assert [call.args for call in store.audit.record.call_args_list] == [
        ("viewer_store_capacity_reached", reason),
    ]


def test_file_read_is_bounded_before_json_parsing(monkeypatch):
    monkeypatch.setattr(viewer_store, "_MAX_STORE_BYTES", 64)
    read = Mock(return_value=b" " * 65)
    handle = SimpleNamespace(read=read)
    opened = Mock()
    opened.__enter__ = Mock(return_value=handle)
    opened.__exit__ = Mock(return_value=False)
    # Simulate a file growing after stat; the actual read still has a hard cap.
    file = SimpleNamespace(open=Mock(return_value=opened), stat=lambda: SimpleNamespace(st_size=64))
    parse = Mock(side_effect=AssertionError("oversized input must not reach JSON parser"))
    monkeypatch.setattr(viewer_store.json, "loads", parse)
    with pytest.raises(viewer_store.ViewerStoreUnavailableError):
        viewer_store._read_profile_file(file, False)
    read.assert_called_once_with(65)
    parse.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
async def test_utf8_write_over_budget_keeps_disk_and_cache_and_does_not_fallback(tmp_path, monkeypatch, custom):
    directory = tmp_path / "custom" if custom else tmp_path
    store = make_store(tmp_path, dir_provider=lambda: str(directory) if custom else "")
    await store.upsert_identity(ViewerIdentity(uid="1", nickname="a"))
    file = directory / "viewer_profiles.json"
    raw = file.read_bytes()
    monkeypatch.setattr(viewer_store, "_MAX_STORE_BYTES", len(raw))
    assert await store.mark_roasted("1", "猫" * 240) is False
    await store.upsert_identity(ViewerIdentity(uid="2", nickname="猫" * 240))
    assert file.read_bytes() == raw
    assert [(item["uid"], item["roast_count"]) for item in await store.recent_profiles()] == [("1", 0)]
    assert store.storage_status()["fallback_active"] is False
    assert store.storage_status()["capacity_reasons"] == ["file_size"]
    assert not list(tmp_path.glob("viewer_profiles-fallback-*.json"))
    assert not list(directory.glob("*.tmp"))
    assert (await store.clear_profiles())["applied"] is True
    assert file.read_bytes() == b"{}"
    assert store.storage_status()["capacity_reasons"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_oversized", [False, True])
async def test_inflight_capacity_failure_belongs_to_its_captured_directory(tmp_path, monkeypatch, legacy_oversized):
    monkeypatch.setattr(viewer_store, "_MAX_PROFILES", 2)
    source = tmp_path / "source"
    source.mkdir()
    file = source / "viewer_profiles.json"
    file.write_text(json.dumps({str(i): {"uid": str(i)} for i in range(3 if legacy_oversized else 2)}), encoding="utf-8")
    selected = [str(source)]
    store = make_store(tmp_path, dir_provider=lambda: selected[0])
    original = store._load_for_write
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def delayed_load(**kwargs):
        entered.set()
        await resume.wait()
        return await original(**kwargs)

    monkeypatch.setattr(store, "_load_for_write", delayed_load)
    task = asyncio.create_task(store.upsert_identity(ViewerIdentity(uid="new", nickname="new")))
    await entered.wait()
    selected[0] = str(tmp_path / "destination")
    assert store.storage_status()["capacity_reasons"] == []
    resume.set()
    assert (await task).uid == "new"
    assert store.storage_status()["capacity_reasons"] == []
