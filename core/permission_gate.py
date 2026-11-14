"""Mode and safety gates for viewer interaction modules."""

from __future__ import annotations

from .contracts import LiveConfig, TriggerSource


def developer_tools_disabled(config: LiveConfig, source: TriggerSource) -> bool:
    return source in {"developer_sandbox", "manual_live_simulation"} and not config.developer_tools_enabled


class PermissionGate:
    def __init__(self, config: LiveConfig) -> None:
        self.config = config

    def update(self, config: LiveConfig) -> None:
        self.config = config

    def allows_source(self, source: TriggerSource) -> tuple[bool, str]:
        if developer_tools_disabled(self.config, source):
            return False, "developer tools are disabled"
        if source == "developer_sandbox":
            return True, ""
        if source in {"manual_live_simulation", "live_danmaku", "idle_hosting", "active_engagement", "warmup_hosting"}:
            if not self.config.live_enabled:
                return False, "live roast is disabled"
            return True, ""
        return False, f"unsupported source: {source}"
