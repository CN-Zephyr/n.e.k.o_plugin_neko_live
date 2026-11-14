"""Namespaced encrypted credential store for live-platform auth.

Credential payloads are Fernet-encrypted under the per-plugin data directory.
On Windows, DPAPI protects the Fernet key with the current user account.
The default ``bili`` namespace keeps the legacy ``bili_credential.*`` filenames;
other providers get isolated ``{namespace}_credential.*`` files. Secrets must
never be written to audit / log / config / UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import windows_dpapi

_KEY_FILE = "bili_credential.key"
_CRED_FILE = "bili_credential.enc"
_FIELDS = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")
_NAMESPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_LEGACY_KEY_RE = re.compile(rb"[A-Za-z0-9_-]{43}=")
_DPAPI_KEY_PREFIX = b"neko-live-dpapi:v1\x00"
_WINDOWS = sys.platform == "win32"


class CredentialDeleteError(OSError):
    """Local credential removal could not be completed."""


class CredentialStore:
    """Encrypted local credential store. save/load/delete run in a worker thread."""

    def __init__(
        self,
        plugin: Any,
        audit: Any = None,
        *,
        namespace: str = "bili",
        fields: tuple[str, ...] = _FIELDS,
    ) -> None:
        self.plugin = plugin
        self.audit = audit
        self.namespace = str(namespace or "bili").strip()
        if not _NAMESPACE_RE.fullmatch(self.namespace):
            raise ValueError("credential namespace must use ASCII letters, digits, '_' or '-'")
        self.fields = tuple(fields or _FIELDS)
        self.audit_identity_field = (
            "DedeUserID"
            if "DedeUserID" in self.fields
            else "uid"
            if "uid" in self.fields
            else ""
        )
        self._lock = asyncio.Lock()
        self._reads_disabled = False

    def _data_dir(self) -> Path:
        return Path(self.plugin.data_path())

    def _key_file(self) -> str:
        return _KEY_FILE if self.namespace == "bili" else f"{self.namespace}_credential.key"

    def _cred_file(self) -> str:
        return _CRED_FILE if self.namespace == "bili" else f"{self.namespace}_credential.enc"

    def _audit_op(self, suffix: str) -> str:
        return f"{self.namespace}_credential_{suffix}"

    @staticmethod
    def _chmod600(path: Path) -> None:
        if not _WINDOWS:
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass

    @staticmethod
    def _read_fernet(key_path: Path):
        from cryptography.fernet import Fernet

        stored = key_path.read_bytes()
        if not _WINDOWS:
            return Fernet(stored), None
        if stored.startswith(_DPAPI_KEY_PREFIX):
            return Fernet(windows_dpapi.unprotect_key(stored[len(_DPAPI_KEY_PREFIX) :])), None
        if not _LEGACY_KEY_RE.fullmatch(stored):
            raise ValueError("invalid credential key format")
        return Fernet(stored), stored

    @classmethod
    def _protect_existing_key(cls, key_path: Path, legacy_key: bytes | None) -> None:
        if legacy_key is not None:
            cls._write_private_file(key_path, _DPAPI_KEY_PREFIX + windows_dpapi.protect_key(legacy_key))

    def _get_fernet_for_save(self, key_path: Path):
        from cryptography.fernet import Fernet

        if key_path.exists():
            fernet, legacy_key = self._read_fernet(key_path)
            self._protect_existing_key(key_path, legacy_key)
            return fernet

        key = Fernet.generate_key()
        stored = _DPAPI_KEY_PREFIX + windows_dpapi.protect_key(key) if _WINDOWS else key
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(stored)
            self._chmod600(key_path)
            return Fernet(key)
        except FileExistsError:
            fernet, legacy_key = self._read_fernet(key_path)
            self._protect_existing_key(key_path, legacy_key)
            return fernet

    @classmethod
    def _write_private_file(cls, path: Path, data: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            cls._chmod600(temporary)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    # ── 同步实现（在 to_thread 里跑） ──────────────────────────

    def _save_sync(self, payload: dict) -> bool:
        try:
            cred = {key: str(payload.get(key) or "") for key in self.fields}
            data_dir = self._data_dir()
            fernet = self._get_fernet_for_save(data_dir / self._key_file())
            enc = fernet.encrypt(json.dumps(cred, ensure_ascii=False).encode("utf-8"))
            self._write_private_file(data_dir / self._cred_file(), enc)
            self._reads_disabled = False
            return True
        except Exception:
            return False

    def _load_sync(self) -> tuple[dict | None, str]:
        """Return ``(data, failure)``; ``failure`` is an exception type name only."""
        if self._reads_disabled:
            return None, ""
        try:
            data_dir = self._data_dir()
            cred_path = data_dir / self._cred_file()
            key_path = data_dir / self._key_file()
            if not cred_path.exists() or not key_path.exists():
                return None, ""
            fernet, legacy_key = self._read_fernet(key_path)
            dec = fernet.decrypt(cred_path.read_bytes()).decode("utf-8")
            data = json.loads(dec)
            if not isinstance(data, dict):
                return None, "invalid_payload"
            self._protect_existing_key(key_path, legacy_key)
        except Exception as exc:
            return None, type(exc).__name__
        return data, ""

    def _delete_sync(self) -> list[str]:
        self._reads_disabled = True
        removed: list[str] = []
        data_dir = self._data_dir()
        failure: OSError | None = None
        for name in (self._cred_file(), self._key_file()):
            try:
                (data_dir / name).unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                failure = exc
            else:
                removed.append(name)
        if failure is not None:
            raise failure
        return removed

    # ── 异步接口（喂给 bili_auth_service 的回调契约） ─────────────

    async def _run_on_disk(self, operation, *args):
        async with self._lock:
            work = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                # 磁盘操作结束前保持锁，迟到的写入不能覆盖注销。
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not work.cancelled():
                    work.exception()
                raise

    async def save(self, payload: dict) -> bool:
        ok = await self._run_on_disk(self._save_sync, payload)
        if self.audit is not None:
            if ok:
                identity = (
                    " ".join(str(payload.get(self.audit_identity_field) or "").split())
                    if self.audit_identity_field
                    else ""
                )
                self.audit.record(
                    self._audit_op("saved"),
                    "credential saved (encrypted)"
                    if identity
                    else "credential saved (encrypted; identity unavailable)",
                    level="info" if identity else "warning",
                    detail={"uid": identity} if identity else {"identity_status": "unidentified"},
                )
            else:
                self.audit.record(self._audit_op("save_failed"), "encrypt/save failed", level="warning")
        return ok

    async def load(self) -> dict | None:
        data, failure = await self._run_on_disk(self._load_sync)
        # Missing files mean "never logged in"; anything else is a damaged or
        # undecryptable store that would otherwise look identical in the UI.
        if failure and self.audit is not None:
            self.audit.record(
                self._audit_op("load_failed"),
                "decrypt/load failed; treated as logged out",
                level="warning",
                detail={"error_type": failure},
            )
        return data

    async def delete(self) -> list[str]:
        try:
            removed = await self._run_on_disk(self._delete_sync)
        except OSError as exc:
            if self.audit is not None:
                self.audit.record(
                    self._audit_op("delete_failed"),
                    "local credential files could not be removed",
                    level="warning",
                    detail={"error_type": type(exc).__name__},
                )
            raise CredentialDeleteError("Local credential removal failed. Retry sign-out.") from None
        if self.audit is not None and removed:
            self.audit.record(self._audit_op("deleted"), "credential files removed", detail={"files": removed})
        return removed

    def has_credential(self) -> bool:
        if self._reads_disabled:
            return False
        try:
            data_dir = self._data_dir()
            return (data_dir / self._cred_file()).exists() and (data_dir / self._key_file()).exists()
        except Exception:
            return False

    async def build_credential(self):
        """构建 `bilibili_api.Credential` 供身份/连接/查询用；无凭据或缺 SESSDATA 返回 None。"""
        data = await self.load()
        if not data or not data.get("SESSDATA"):
            return None
        try:
            from bilibili_api import Credential

            return Credential(
                sessdata=data.get("SESSDATA", ""),
                bili_jct=data.get("bili_jct", ""),
                dedeuserid=str(data.get("DedeUserID", "") or ""),
                buvid3=data.get("buvid3", ""),
            )
        except Exception:
            return None
