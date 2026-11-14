"""Replaceable Douyin bridge backend specification."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_DOUYIN_LIVE_SHA256 = "14b21b9682dc8f1f510f5390b640cf1a07f2597ab7a77e0e705ec3852e83ccf2"


@dataclass(frozen=True, slots=True)
class DouyinBridgeBackend:
    """Spec for one bundled local bridge executable."""

    backend_id: str
    executable_path: Path
    args_factory: Callable[[int], list[str]]
    expected_sha256: str
    supported: bool = True
    process_factory: Callable[..., Any] | None = None
    listener_owner: Callable[[Any, int], bool] | None = None


def default_douyin_bridge_backend() -> DouyinBridgeBackend:
    return DouyinBridgeBackend(
        backend_id="douyinlive",
        executable_path=_bundled_executable_path("douyinLive.exe"),
        args_factory=lambda port: ["--port", str(port), "--log-level", "warn"],
        expected_sha256=_DOUYIN_LIVE_SHA256,
        supported=sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"},
        process_factory=_start_windows_bridge,
        listener_owner=_owns_windows_listener,
    )


def _bundled_executable_path(filename: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "vendor" / "douyin_bridge" / "windows-amd64" / filename


def _start_windows_bridge(*args, **kwargs):
    from ..live_bridge.windows_job import start_job_process

    return start_job_process(*args, **kwargs)


def _owns_windows_listener(process, port: int) -> bool:
    from ..live_bridge.windows_listener import owns_listener

    return owns_listener(process.pid, port)
