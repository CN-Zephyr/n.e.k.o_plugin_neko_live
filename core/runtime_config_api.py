"""Runtime compatibility API for configuration actions."""

from __future__ import annotations

from typing import Any

from . import runtime_config
from .contracts import LiveConfig


class RuntimeConfigApiMixin:
    async def reload_config(self) -> LiveConfig:
        return await runtime_config.reload_config(self)

    async def update_config(self, updates: dict[str, Any]) -> LiveConfig:
        return await runtime_config.update_config(self, updates)

    async def _start_live_listener(self, room_ref: Any) -> bool:
        return await runtime_config.start_live_listener(self, room_ref)

    async def _stop_live_listener(self, *, mark_disabled: bool) -> None:
        await runtime_config.stop_live_listener(self, mark_disabled=mark_disabled)
