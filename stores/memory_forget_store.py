"""Bounded, durable checkpoints for explicit host-memory erasure."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from filelock import FileLock

MAX_REQUESTS = 64
MAX_BYTES = 64 * 1024
_PHASES = ("prepared", "host_confirmed", "local_deleted")
Owner = tuple[str, str, str]


def subject_owner(target: str, platform: str, uid: str) -> Owner:
    return target, platform, uid if uid.startswith(platform + ":") else platform + ":" + uid


@dataclass(frozen=True)
class ForgetRequest:
    target: str
    platform: str
    uid: str
    epoch: int
    port: int
    profile_scope: str
    phase: str = "prepared"

    @property
    def owner(self) -> Owner:
        return subject_owner(self.target, self.platform, self.uid)


def _request(value: Any) -> ForgetRequest:
    if not isinstance(value, dict) or set(value) != set(ForgetRequest.__dataclass_fields__):
        raise ValueError("invalid request")
    request = ForgetRequest(**value)
    if (
        not isinstance(request.target, str) or not 1 <= len(request.target) <= 128 or not request.target.isprintable()
        or request.platform not in {"bilibili", "douyin", "twitch"}
        or not isinstance(request.uid, str)
        or not re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", request.uid.removeprefix(request.platform + ":"))
        or type(request.epoch) is not int or not 1 <= request.epoch < 2**63
        or type(request.port) is not int or not 1 <= request.port <= 65535
        or not isinstance(request.profile_scope, str) or not re.fullmatch(r"[0-9a-f]{64}", request.profile_scope)
        or request.phase not in _PHASES
    ):
        raise ValueError("invalid request")
    return request


class MemoryForgetStore:
    """Atomic journal in plugin data; no network or automatic erase on restore."""

    def __init__(self, plugin: Any, audit: Any = None):
        self.path: Path | None = None
        self.entries: dict[Owner, ForgetRequest] = {}
        self.available = False
        self.audit = audit
        self._warned = False
        self._lock = asyncio.Lock()
        try:
            # No temp-directory fallback: missing ownership must fail closed.
            self.path = Path(plugin.data_path()).resolve() / "memory_forget_pending.json"
            self.entries = self._read()
            self.available = True
        except Exception:
            self._unavailable()

    def _unavailable(self) -> None:
        self.available = False
        if not self._warned and self.audit is not None:
            self.audit.record("memory_forget_store_unavailable", "memory erasure journal unavailable", level="warning")
        self._warned = True

    def _read(self) -> dict[Owner, ForgetRequest]:
        if self.path is None or self.path.is_symlink():
            raise OSError("invalid journal location")
        try:
            with self.path.open("rb") as handle:
                data = handle.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {}
        if len(data) > MAX_BYTES:
            raise ValueError("journal too large")
        document = json.loads(data)
        if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
            raise ValueError("invalid journal version")
        rows = document.get("requests")
        if not isinstance(rows, list) or len(rows) > MAX_REQUESTS:
            raise ValueError("invalid journal requests")
        records = [_request(row) for row in rows]
        entries = {item.owner: item for item in records}
        if len(entries) != len(rows):
            raise ValueError("duplicate journal owner")
        return entries

    def _write(self, entries: dict[Owner, ForgetRequest]) -> None:
        data = json.dumps({"version": 1, "requests": [asdict(value) for value in entries.values()]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(entries) > MAX_REQUESTS or len(data) > MAX_BYTES:
            raise OSError("host_memory.too_many_pending_deletions")
        if self.path is None or self.path.is_symlink():
            raise OSError("invalid journal location")
        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".memory_forget-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _change(self, operation: str, request: ForgetRequest | None):
        if self.path is None:
            raise OSError("missing journal location")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        if lock_path.is_symlink():
            raise OSError("invalid journal lock")
        with FileLock(str(lock_path), timeout=1):
            entries = self._read()
            result = None
            if operation != "reload":
                request = _request(asdict(request))
                old = entries.get(request.owner)
                if operation == "reserve":
                    result = (old or request, old is None)
                    if old is None:
                        entries[request.owner] = request
                else:
                    if old is None or replace(old, phase=request.phase) != request:
                        raise OSError("journal request changed")
                    if operation == "remove":
                        if old != request:
                            raise OSError("journal phase changed")
                        entries.pop(request.owner)
                    elif _PHASES.index(request.phase) >= _PHASES.index(old.phase):
                        entries[request.owner] = request
                    else:
                        raise OSError("journal phase moved backwards")
                self._write(entries)
            return entries, result

    async def _run(self, operation: str, request: ForgetRequest | None = None):
        async with self._lock:
            work = asyncio.create_task(asyncio.to_thread(self._change, operation, request))
            cancelled = False
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            try:
                self.entries, result = work.result()
                self.available, self._warned = True, False
            except Exception:
                self._unavailable()
                if cancelled:
                    raise asyncio.CancelledError from None
                raise OSError("host_memory.journal_unavailable") from None
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def reload(self) -> None:
        await self._run("reload")

    async def reserve(self, request: ForgetRequest) -> tuple[ForgetRequest, bool]:
        return await self._run("reserve", request)

    async def checkpoint(self, request: ForgetRequest, phase: str) -> ForgetRequest:
        updated = replace(request, phase=phase)
        await self._run("checkpoint", updated)
        return updated

    async def remove(self, request: ForgetRequest) -> None:
        await self._run("remove", request)

    def blocks(self, owner: Owner) -> bool:
        return not self.available or owner in self.entries

    def blocks_profile(self, uid: str) -> bool:
        # Keep the local deletion checkpoint retryable after a crash between
        # the profile commit and journal checkpoint. Do not recreate that UID.
        return not self.available or any(
            uid in (item.uid, item.owner[2], item.uid.removeprefix(item.platform + ":"))
            for item in self.entries.values()
        )

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "pending": [
            {"uid": item.uid, "platform": item.platform, "target": item.target, "phase": item.phase}
            for item in self.entries.values()
        ]}
