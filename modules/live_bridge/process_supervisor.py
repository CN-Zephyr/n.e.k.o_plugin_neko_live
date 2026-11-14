"""Small process supervisor for bundled local bridge executables."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import socket
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class BridgeProcessState:
    ok: bool = False
    base_url: str = ""
    port: int = 0
    last_error: str = ""


class BridgeProcessSupervisor:
    """Start and stop one localhost bridge executable."""

    def __init__(
        self,
        *,
        executable_path: Path,
        args_factory: Callable[[int], list[str]],
        expected_sha256: str | None = None,
        process_factory: Any = None,
        port_factory: Callable[[], int] | None = None,
        port_waiter: Callable[[int, float], bool] | None = None,
        listener_owner: Callable[[Any, int], bool] | None = None,
    ) -> None:
        self._executable_path = executable_path
        self._args_factory = args_factory
        self._expected_sha256 = expected_sha256
        self._process_factory = process_factory or subprocess.Popen
        self._port_factory = port_factory or _find_free_port
        self._port_waiter = port_waiter or _wait_for_port
        self._listener_owner = listener_owner
        self._process: Any = None
        self._port = 0
        self._ready = False
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> BridgeProcessState:
        async with self._lifecycle_lock:
            return await self._start_locked()

    async def _start_locked(self) -> BridgeProcessState:
        if self._ready and self._is_running():
            return BridgeProcessState(ok=True, base_url=_base_url(self._port), port=self._port)
        if self._process is not None:
            stopped = await self._stop_locked()
            if stopped.last_error:
                return stopped
        if not self._executable_path.is_file():
            return BridgeProcessState(ok=False, last_error="bundled bridge executable is missing")
        if self._expected_sha256 is not None:
            checksum_error = await asyncio.to_thread(
                _bridge_checksum_error,
                self._executable_path,
                self._expected_sha256,
            )
            if checksum_error:
                return BridgeProcessState(ok=False, last_error=checksum_error)
        try:
            port = self._port_factory()
            self._process = self._process_factory(
                [str(self._executable_path), *self._args_factory(port)],
                cwd=str(self._executable_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creation_flags(),
            )
        except Exception as exc:
            return BridgeProcessState(ok=False, last_error=f"bundled bridge start failed: {type(exc).__name__}")
        self._port = port
        try:
            if self._listener_owner is None:
                listening = await asyncio.to_thread(self._port_waiter, port, 8.0)
            else:
                listening = await self._wait_for_owned_port(self._listener_owner, self._process, port)
            self._ready = listening and self._is_running()
            if self._ready:
                return BridgeProcessState(ok=True, base_url=_base_url(port), port=port)
            error = (
                "bundled bridge exited during startup" if not self._is_running()
                else "bundled bridge did not open localhost port"
            )
        except OSError:
            error = "bundled bridge localhost listener could not be verified"
        except BaseException:
            await self._stop_locked()
            raise
        stopped = await self._stop_locked()
        return BridgeProcessState(ok=False, last_error=stopped.last_error or error)

    @staticmethod
    async def _wait_for_owned_port(listener_owner: Callable[[Any, int], bool], process: Any, port: int) -> bool:
        deadline = time.monotonic() + 8.0
        while _poll(process) is None:
            owns_port = await asyncio.to_thread(listener_owner, process, port)
            if owns_port and await asyncio.to_thread(_connect_local_port, port):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.1, remaining))
        return False

    async def stop(self) -> BridgeProcessState:
        async with self._lifecycle_lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> BridgeProcessState:
        process = self._process
        self._ready = False
        port = self._port
        stopped = True
        if process is not None and _poll(process) is None:
            with suppress(Exception):
                process.terminate()
            stopped = await asyncio.to_thread(_wait_for_process_exit, process)
        if stopped:
            self._process = None
            self._port = 0
        return BridgeProcessState(
            ok=False,
            port=port,
            last_error="" if stopped else "bundled bridge process did not stop",
        )

    def _is_running(self) -> bool:
        return self._process is not None and self._port > 0 and _poll(self._process) is None


def _bridge_checksum_error(executable_path: Path, expected: str) -> str:
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        return "bundled bridge checksum is invalid"

    try:
        digest = hashlib.sha256()
        with executable_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "bundled bridge checksum could not be verified"
    if not hmac.compare_digest(digest.hexdigest(), expected):
        return "bundled bridge checksum mismatch"
    return ""


def _poll(process: Any) -> Any:
    try:
        return process.poll()
    except Exception:
        return 0


def _wait_for_process_exit(process: Any) -> bool:
    with suppress(Exception):
        process.wait(timeout=4)
    if _poll(process) is None:
        with suppress(Exception):
            process.kill()
            process.wait(timeout=2)
    return _poll(process) is not None


def _base_url(port: int) -> str:
    return f"ws://127.0.0.1:{int(port)}/ws" if int(port) > 0 else ""


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        if _connect_local_port(port):
            return True
        time.sleep(0.1)
    return False


def _connect_local_port(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
