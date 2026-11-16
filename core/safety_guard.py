"""Runtime safety guard for live viewer interactions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from . import safety_guard_cooldown, safety_guard_failures
from .contracts import LiveConfig, SafetyDecision, SafetyStatus, ViewerEvent
from .permission_gate import developer_tools_disabled
from .safety_guard_input import ViewerInputLimiter
from .safety_guard_types import FailureKind

_LIVE_CONNECTED_SOURCES = {
    "live_danmaku",
    "manual_live_simulation",
    "idle_hosting",
    "active_engagement",
    "warmup_hosting",
}


def _requires_live_connection(event: ViewerEvent | None) -> bool:
    if event is None:
        return False
    return event.source in _LIVE_CONNECTED_SOURCES


@dataclass
class SafetyGuard:
    """Public safety gate facade for stream input, output, and circuit breaks."""

    config: LiveConfig
    audit: Any
    manual_paused: bool = False
    auto_paused: bool = False
    degraded: bool = False
    connected: bool = True
    queue_size: int = 0
    queue_overflows: int = 0
    _viewer_inputs: ViewerInputLimiter = field(default_factory=ViewerInputLimiter, repr=False)
    _pipeline_failures: list[float] = field(default_factory=list)
    _output_failures: list[float] = field(default_factory=list)
    _last_output_at: float = 0.0
    _last_support_output_at: float = 0.0
    _queue_generation: int = 0
    _event_tasks: dict[asyncio.Task[Any], int] = field(default_factory=dict, repr=False)
    _capacity_waiters: dict[asyncio.Task[Any], int] = field(default_factory=dict, repr=False)
    _capacity_changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def update(self, config: LiveConfig) -> None:
        self.config = config
        self._capacity_changed.set()

    def pause(self, reason: str = "manual pause") -> None:
        self.manual_paused = True
        self._capacity_changed.set()
        self.audit.record("safety_manual_pause", reason, level="warning")

    def resume(self) -> None:
        self.manual_paused = False
        self.auto_paused = False
        self.degraded = False
        self.queue_overflows = 0
        self._pipeline_failures.clear()
        self._output_failures.clear()
        self._last_output_at = 0.0
        self._last_support_output_at = 0.0
        self.clear_queue()
        self.audit.record("safety_resumed", "safety guard reset", level="info")

    def clear_queue(self) -> None:
        self._queue_generation += 1
        current = _current_task()
        pending = [task for task in self._event_tasks if not task.done()]
        waiting = [task for task in self._capacity_waiters if not task.done()]
        for task in {*pending, *waiting}:
            if task is not current:
                task.cancel()
        # Cancelling work still owns its slot until after_event(). Resetting
        # the count early would allow new work to overtake its finalization.
        if not pending:
            self.queue_size = 0
        self._capacity_changed.set()

    def set_connected(self, connected: bool) -> None:
        if not connected:
            self._viewer_inputs.clear()
        self.connected = bool(connected)
        self._capacity_changed.set()

    @asynccontextmanager
    async def wait_for_capacity(self) -> AsyncIterator[None]:
        """Let the bounded support worker wait upstream of normal admission.

        The worker gets the next free slot, but must still call before_event()
        through the pipeline. This context owns neither a new task nor a slot.
        """
        task = asyncio.current_task()
        generation = self._queue_generation
        self._capacity_waiters[task] = generation
        try:
            while self.queue_size >= self.config.queue_limit:
                if generation != self._queue_generation:
                    raise asyncio.CancelledError
                if self.manual_paused or self.auto_paused or not self.connected:
                    break
                self._capacity_changed.clear()
                await self._capacity_changed.wait()
            if generation != self._queue_generation:
                raise asyncio.CancelledError
            yield
        finally:
            self._capacity_waiters.pop(task, None)
            self._capacity_changed.set()

    def before_event(self, event: ViewerEvent) -> SafetyDecision:
        task = _current_task()
        waiting_generation = self._capacity_waiters.get(task)
        if waiting_generation is not None and waiting_generation != self._queue_generation:
            return SafetyDecision(False, self.status(), "queue.cleared")
        if _requires_live_connection(event) and not self.connected:
            return SafetyDecision(
                False, "disconnected", "live event source is disconnected"
            )
        if self.manual_paused:
            return SafetyDecision(False, "paused", "roast is manually paused")
        if self.auto_paused:
            return SafetyDecision(False, "tripped", "automatic safety stop is active")
        if not self._viewer_inputs.allows(event, self.config):
            return SafetyDecision(False, "cooldown", "input.viewer_rate_limited")
        admission_limit = self.config.queue_limit
        if waiting_generation is None and self._queue_generation in self._capacity_waiters.values():
            # Reserve the next free slot only while a support worker is waiting.
            admission_limit = max(0, admission_limit - 1)
        if self.queue_size >= admission_limit:
            self.queue_overflows += 1
            if self.queue_overflows >= self.config.safety_queue_overflow_limit:
                self.degraded = True
            self.audit.record(
                "safety_queue_overflow",
                "queue limit reached",
                level="warning",
                detail={
                    "queue_size": self.queue_size,
                    "queue_limit": self.config.queue_limit,
                    "admission_limit": admission_limit,
                },
            )
            return SafetyDecision(False, self.status(), "queue limit reached")
        self.queue_size += 1
        self._capacity_waiters.pop(task, None)
        if task is not None:
            self._event_tasks[task] = self._queue_generation
        return SafetyDecision(True, self.status(), "")

    def after_event(self) -> None:
        task = _current_task()
        if task is not None:
            self._event_tasks.pop(task, None)
        self.queue_size = max(0, self.queue_size - 1)
        self._capacity_changed.set()

    def before_output(
        self,
        event: ViewerEvent | None = None,
        *,
        consume_cooldown: bool = True,
    ) -> SafetyDecision:
        """Check the final output gates.

        ``consume_cooldown=False`` is reserved for probe/inspection paths: it
        skips cooldown validation and does not advance ``_last_output_at``.
        Real ``push_roast`` output must keep the default value.
        """
        generation = self._event_tasks.get(_current_task())
        if generation is not None and generation != self._queue_generation:
            return SafetyDecision(False, self.status(), "queue.cleared")
        # Developer permissions can change while the pipeline awaits identity.
        if event is not None and developer_tools_disabled(self.config, event.source):
            return SafetyDecision(False, self.status(), "permission.developer_tools_disabled")
        if _requires_live_connection(event) and not self.connected:
            return SafetyDecision(
                False, "disconnected", "live output source is disconnected"
            )
        if self.manual_paused:
            return SafetyDecision(False, "paused", "output is manually paused")
        if self.auto_paused:
            return SafetyDecision(False, "tripped", "automatic safety stop is active")
        if consume_cooldown:
            cooldown_decision = safety_guard_cooldown.before_output_cooldown(self, event)
            if cooldown_decision is not None:
                return cooldown_decision
        return SafetyDecision(True, self.status(), "")

    def output_cooldown_remaining(self, now: float | None = None) -> float:
        """Return seconds until output cooldown opens; pause/trip are separate gates."""
        return safety_guard_cooldown.output_cooldown_remaining(self, now)

    def record_failure(self, kind: FailureKind, message: str) -> None:
        safety_guard_failures.record_failure(self, kind, message)
        self._capacity_changed.set()

    def status(self) -> SafetyStatus:
        if not self.connected:
            return "disconnected"
        if self.auto_paused:
            return "tripped"
        if self.manual_paused:
            return "paused"
        if self.degraded:
            return "degraded"
        return "running"

    def snapshot(self) -> dict[str, Any]:
        safety_guard_failures.prune_failure_buckets(self)
        return {
            "status": self.status(),
            "manual_paused": self.manual_paused,
            "auto_paused": self.auto_paused,
            "auto_stop_enabled": self.config.safety_auto_stop_enabled,
            "degraded": self.degraded,
            "connected": self.connected,
            "queue_size": self.queue_size,
            "queue_limit": self.config.queue_limit,
            "queue_overflows": self.queue_overflows,
            "viewer_input_rejected": self._viewer_inputs.rejected,
            "waiting_for_capacity": sum(generation == self._queue_generation for generation in self._capacity_waiters.values()),
            "pipeline_failures": len(self._pipeline_failures),
            "output_failures": len(self._output_failures),
        }


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
