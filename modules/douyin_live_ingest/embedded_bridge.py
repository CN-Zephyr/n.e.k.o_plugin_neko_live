"""Bundled Douyin bridge supervisor."""

from __future__ import annotations

from ..live_bridge.process_supervisor import BridgeProcessState, BridgeProcessSupervisor
from .bridge_backend import DouyinBridgeBackend, default_douyin_bridge_backend


class DouyinEmbeddedBridgeSupervisor:
    """Owns the selected bundled Douyin bridge executable lifecycle."""

    def __init__(
        self,
        *,
        supervisor: BridgeProcessSupervisor | None = None,
        backend: DouyinBridgeBackend | None = None,
    ) -> None:
        backend = backend or default_douyin_bridge_backend()
        self.backend_id = backend.backend_id
        self._backend = backend
        self._supervisor = supervisor or BridgeProcessSupervisor(
            executable_path=backend.executable_path,
            args_factory=backend.args_factory,
            expected_sha256=backend.expected_sha256,
            process_factory=backend.process_factory,
            listener_owner=backend.listener_owner,
        )

    def is_available(self) -> bool:
        try:
            return self._backend.supported and self._backend.executable_path.is_file()
        except OSError:
            return False

    async def start(self) -> BridgeProcessState:
        if not self._backend.supported:
            return BridgeProcessState(last_error="bundled bridge requires Windows x64")
        return await self._supervisor.start()

    async def stop(self) -> BridgeProcessState:
        return await self._supervisor.stop()
