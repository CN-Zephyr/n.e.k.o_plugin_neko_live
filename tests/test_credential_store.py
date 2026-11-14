"""CredentialStore（P5 登录态）单测：加解密往返、落盘为密文、退出登录删文件。"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from plugin.plugins.neko_live.stores.credential_store import CredentialDeleteError, CredentialStore
from plugin.plugins.neko_live.stores.windows_dpapi import unprotect_key


class _FakePlugin:
    def __init__(self, data_dir):
        self._data_dir = data_dir

    def data_path(self, *parts):
        return self._data_dir.joinpath(*parts) if parts else self._data_dir


class _Audit:
    def __init__(self):
        self.events = []

    def record(self, op, message, **kwargs):
        self.events.append({"op": op, "message": message, **kwargs})


@pytest.mark.skipif(sys.platform != "win32", reason="requires current-user Windows DPAPI")
@pytest.mark.parametrize("legacy", [False, True], ids=["fresh-key", "legacy-key"])
@pytest.mark.asyncio
async def test_windows_credentials_are_protected_or_migrated(tmp_path, legacy):
    from cryptography.fernet import Fernet

    plugin = _FakePlugin(tmp_path)
    store = CredentialStore(plugin)
    key_path = tmp_path / "bili_credential.key"
    enc_path = tmp_path / "bili_credential.enc"
    old_key = None
    if legacy:
        old_key = Fernet.generate_key()
        key_path.write_bytes(old_key)
        original = Fernet(old_key).encrypt(b'{"SESSDATA":"synthetic-legacy"}')
        enc_path.write_bytes(original)
        assert (await store.load())["SESSDATA"] == "synthetic-legacy"
        assert enc_path.read_bytes() == original
    else:
        assert await store.save({"SESSDATA": "synthetic-fresh"}) is True

    prefix, protected = key_path.read_bytes().split(b"\x00", 1)
    assert prefix == b"neko-live-dpapi:v1"
    key = unprotect_key(protected)
    assert key not in protected
    if old_key is not None:
        assert key == old_key
    expected = "synthetic-legacy" if legacy else "synthetic-fresh"
    assert (await CredentialStore(plugin).load())["SESSDATA"] == expected


@pytest.mark.skipif(sys.platform != "win32", reason="requires current-user Windows DPAPI")
@pytest.mark.asyncio
async def test_windows_migration_failure_keeps_legacy_files(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    key_path = tmp_path / "bili_credential.key"
    enc_path = tmp_path / "bili_credential.enc"
    key = Fernet.generate_key()
    ciphertext = Fernet(key).encrypt(b'{"SESSDATA":"synthetic-legacy"}')
    key_path.write_bytes(key)
    enc_path.write_bytes(ciphertext)
    audit = _Audit()
    store = CredentialStore(_FakePlugin(tmp_path), audit=audit)

    def deny_replace(*_args):
        raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", deny_replace)
        assert await store.load() is None
        assert await store.save({"SESSDATA": "synthetic-new"}) is False

    assert audit.events[0]["detail"] == {"error_type": "PermissionError"}
    assert key_path.read_bytes() == key
    assert enc_path.read_bytes() == ciphertext
    assert not list(tmp_path.glob("*.tmp"))
    assert (await store.load())["SESSDATA"] == "synthetic-legacy"
    assert key_path.read_bytes() != key


@pytest.mark.asyncio
async def test_credential_save_load_roundtrip_and_encrypted_at_rest(tmp_path):
    pytest.importorskip("cryptography")
    store = CredentialStore(_FakePlugin(tmp_path), audit=None)

    assert store.has_credential() is False
    assert await store.load() is None

    ok = await store.save(
        {"SESSDATA": "sess-secret", "bili_jct": "jct", "DedeUserID": "42", "buvid3": "buv", "extra": "drop-me"}
    )
    assert ok is True
    assert store.has_credential() is True

    data = await store.load()
    assert data["SESSDATA"] == "sess-secret"
    assert data["DedeUserID"] == "42"
    assert "extra" not in data  # 只保留已知字段

    # 落盘必须是密文：原始明文不出现在文件里
    enc_bytes = (tmp_path / "bili_credential.enc").read_bytes()
    assert b"sess-secret" not in enc_bytes


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
@pytest.mark.asyncio
async def test_credential_files_are_created_private(tmp_path):
    pytest.importorskip("cryptography")
    store = CredentialStore(_FakePlugin(tmp_path), audit=None)

    assert await store.save({"SESSDATA": "secret"}) is True

    for name in ("bili_credential.key", "bili_credential.enc"):
        mode = stat.S_IMODE((tmp_path / name).stat().st_mode)
        assert mode == 0o600


@pytest.mark.asyncio
async def test_credential_replace_failure_preserves_existing_ciphertext(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    store = CredentialStore(_FakePlugin(tmp_path), audit=None)
    assert await store.save({"SESSDATA": "old-secret"}) is True
    original = (tmp_path / "bili_credential.enc").read_bytes()

    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("denied")))

    assert await store.save({"SESSDATA": "new-secret"}) is False
    assert (tmp_path / "bili_credential.enc").read_bytes() == original
    assert not list(tmp_path.glob(".bili_credential.enc.*.tmp"))


@pytest.mark.asyncio
async def test_credential_delete_removes_files(tmp_path):
    pytest.importorskip("cryptography")
    store = CredentialStore(_FakePlugin(tmp_path), audit=None)
    await store.save({"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "1", "buvid3": "z"})
    assert store.has_credential() is True

    removed = await store.delete()

    assert "bili_credential.enc" in removed
    assert "bili_credential.key" in removed
    assert store.has_credential() is False
    assert await store.load() is None


@pytest.mark.parametrize("platform, namespace, action, cache", [
    ("bilibili", "bili", "bili_logout", "bili_credential"),
    ("douyin", "douyin", "douyin_cookie_delete", "douyin_credential"),
    ("twitch", "twitch", "twitch_logout", "twitch_credential"),
])
@pytest.mark.asyncio
async def test_logout_reports_local_removal_failure_and_still_stops_provider(
    runtime, tmp_path, monkeypatch, platform, namespace, action, cache,
):
    store = runtime.credential_store if namespace == "bili" else getattr(runtime, f"{namespace}_credential_store")
    assert await store.save({})
    setattr(runtime, cache, object())
    runtime.config.live_platform = platform
    runtime.config.live_enabled = True
    stopped = []

    async def stop(*, mark_disabled):
        stopped.append(mark_disabled)
        runtime.config.live_enabled = False

    monkeypatch.setattr(runtime, "_stop_live_listener", stop)
    original = Path.unlink

    def blocked(path, *args, **kwargs):
        if path.name.startswith(f"{namespace}_credential."):
            raise PermissionError("synthetic-private-path")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", blocked)
        result = await getattr(runtime, action)()

    assert result["logged_out"] is False
    assert result["logout_error"] == "credential_removal_failed"
    assert stopped == [True]
    assert not runtime.config.live_enabled
    assert getattr(runtime, cache) is None
    assert (tmp_path / f"{namespace}_credential.enc").exists()
    assert await store.load() is None
    assert "synthetic-private-path" not in str(runtime.audit.recent())
    assert (await getattr(runtime, action)())["logged_out"] is True
    assert not (tmp_path / f"{namespace}_credential.enc").exists()


@pytest.mark.asyncio
async def test_cancelled_worker_finishes_before_credential_delete(tmp_path, monkeypatch):
    store = CredentialStore(_FakePlugin(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original = store._save_sync

    def blocked_save(payload):
        entered.set()
        assert release.wait(5)
        return original(payload)

    monkeypatch.setattr(store, "_save_sync", blocked_save)
    saving = asyncio.create_task(store.save({"SESSDATA": "synthetic"}))
    assert await asyncio.to_thread(entered.wait, 5)
    saving.cancel()
    deleting = asyncio.create_task(store.delete())
    try:
        await asyncio.sleep(0)
        assert not deleting.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await saving
    assert len(await deleting) == 2
    assert not (tmp_path / "bili_credential.enc").exists()


@pytest.mark.asyncio
async def test_partial_removal_can_be_retried(tmp_path, monkeypatch):
    store = CredentialStore(_FakePlugin(tmp_path))
    assert await store.save({})
    original = Path.unlink

    def block_key(path, *args, **kwargs):
        if path.name == "bili_credential.key":
            raise PermissionError("synthetic")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", block_key)
        with pytest.raises(CredentialDeleteError, match="Retry sign-out"):
            await store.delete()
    assert not (tmp_path / "bili_credential.enc").exists()
    assert await store.delete() == ["bili_credential.key"]


@pytest.mark.asyncio
async def test_logout_retry_reports_listener_failure_after_files_are_removed(runtime, monkeypatch):
    assert await runtime.douyin_credential_store.save({})
    runtime.config.live_platform = "douyin"

    async def stop_fails(*, mark_disabled):
        assert mark_disabled
        raise OSError("synthetic-private-listener-error")

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_stop_live_listener", stop_fails)
        result = await runtime.douyin_cookie_delete()
    assert result["logged_out"] is False
    assert result["logout_error"] == "listener_stop_failed"
    assert len(result["removed"]) == 2
    assert "synthetic-private-listener-error" not in str(runtime.audit.recent())
    assert (await runtime.douyin_cookie_delete())["logged_out"] is True


@pytest.mark.asyncio
async def test_namespaced_credentials_are_isolated(tmp_path):
    pytest.importorskip("cryptography")
    plugin = _FakePlugin(tmp_path)
    audit = _Audit()
    bili = CredentialStore(plugin, audit=None)
    douyin = CredentialStore(
        plugin,
        audit=audit,
        namespace="douyin",
        fields=("sessionid", "ttwid", "uid"),
    )

    assert await bili.save({"SESSDATA": "bili-secret"}) is True
    assert await douyin.save(
        {"sessionid": "douyin-secret", "ttwid": "tw", "uid": "douyin-user"}
    ) is True

    assert (await bili.load())["SESSDATA"] == "bili-secret"
    assert (await douyin.load())["sessionid"] == "douyin-secret"
    assert (tmp_path / "bili_credential.enc").exists()
    assert (tmp_path / "douyin_credential.enc").exists()
    assert audit.events[-1]["detail"]["uid"] == "douyin-user"
    assert audit.events[-1]["level"] == "info"


@pytest.mark.asyncio
async def test_credential_save_marks_missing_audit_identity_as_unidentified(tmp_path):
    pytest.importorskip("cryptography")
    audit = _Audit()
    store = CredentialStore(
        _FakePlugin(tmp_path),
        audit=audit,
        namespace="douyin",
        fields=("cookie", "uid"),
    )

    assert await store.save({"cookie": "ttwid=secret-cookie", "uid": "  "}) is True
    assert (await store.load())["cookie"] == "ttwid=secret-cookie"
    assert audit.events[-1]["op"] == "douyin_credential_saved"
    assert audit.events[-1]["level"] == "warning"
    assert audit.events[-1]["detail"] == {"identity_status": "unidentified"}
    assert "secret-cookie" not in str(audit.events[-1])


def test_invalid_credential_namespace_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        CredentialStore(_FakePlugin(tmp_path), namespace="bili!!")


@pytest.mark.asyncio
async def test_credential_store_namespace_keeps_douyin_cookie_separate(tmp_path):
    pytest.importorskip("cryptography")
    store = CredentialStore(
        _FakePlugin(tmp_path),
        audit=None,
        namespace="douyin",
        fields=("cookie", "uid", "nickname", "saved_at"),
    )

    ok = await store.save(
        {
            "cookie": "ttwid=secret-cookie; odin_tt=hidden",
            "uid": "42",
            "nickname": "dy-user",
            "saved_at": "now",
            "SESSDATA": "drop-me",
        }
    )

    assert ok is True
    assert store.has_credential() is True
    assert (tmp_path / "douyin_credential.enc").exists()
    assert (tmp_path / "douyin_credential.key").exists()
    assert not (tmp_path / "bili_credential.enc").exists()
    data = await store.load()
    assert data == {
        "cookie": "ttwid=secret-cookie; odin_tt=hidden",
        "uid": "42",
        "nickname": "dy-user",
        "saved_at": "now",
    }
    assert b"secret-cookie" not in (tmp_path / "douyin_credential.enc").read_bytes()


@pytest.mark.asyncio
async def test_credential_load_failure_is_audited_without_secret_material(tmp_path):
    pytest.importorskip("cryptography")
    audit = _Audit()
    store = CredentialStore(_FakePlugin(tmp_path), audit=audit)
    assert await store.save({"SESSDATA": "sess-secret", "DedeUserID": "42"}) is True
    audit.events.clear()

    # A foreign key can no longer decrypt the stored token.
    from cryptography.fernet import Fernet

    (tmp_path / "bili_credential.key").write_bytes(Fernet.generate_key())

    assert await store.load() is None
    assert store.has_credential() is True
    assert [event["op"] for event in audit.events] == ["bili_credential_load_failed"]
    detail = audit.events[0]["detail"]
    assert detail == {"error_type": "InvalidToken"}
    assert "sess-secret" not in repr(audit.events)


@pytest.mark.asyncio
async def test_credential_missing_files_load_is_silent(tmp_path):
    audit = _Audit()
    store = CredentialStore(_FakePlugin(tmp_path), audit=audit)

    assert await store.load() is None
    assert audit.events == []
