from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from plugin.plugins.neko_live.core import safety_guard_input
from plugin.plugins.neko_live.core.contracts import LiveConfig, ViewerEvent
from plugin.plugins.neko_live.core.safety_guard_input import ViewerInputLimiter


def event(uid="7", kind="danmaku", source="live_danmaku"):
    return ViewerEvent(uid=uid, source=source, danmaku_text="hello", raw={"event_type": kind})


def test_window_is_per_viewer_and_room_without_evicting_active_identities(monkeypatch, patch_module_clock):
    now = [100.0]
    patch_module_clock(monkeypatch, safety_guard_input, monotonic=lambda: now[0])
    monkeypatch.setattr(safety_guard_input, "MAX_VIEWERS", 2)
    limiter, config = ViewerInputLimiter(), LiveConfig()
    assert all(limiter.allows(event(), config) for _ in range(5))
    assert not limiter.allows(event(), config)
    assert limiter.allows(event("8"), config)
    assert not limiter.allows(event("9"), config)
    assert not limiter.allows(event(), config)
    assert len(limiter._viewers) == 2
    now[0] = 110.0
    assert limiter.allows(event(), config)
    config.live_room_ref = "another-room"
    assert all(limiter.allows(event(), config) for _ in range(5))
    assert not limiter.allows(event(), config)


@pytest.mark.parametrize("kind,source", [("gift", "live_danmaku"), ("super_chat", "live_danmaku"), ("guard", "live_danmaku"), ("danmaku", "live_support_events"), ("danmaku", "developer_sandbox")])
def test_support_and_offline_sandbox_do_not_spend_chat_quota(kind, source):
    limiter, config = ViewerInputLimiter(), LiveConfig()
    assert all(limiter.allows(event(kind=kind, source=source), config) for _ in range(20))
    assert not limiter._viewers


@pytest.mark.asyncio
async def test_guard_rejects_before_identity_and_profile_io_and_resets_on_disconnect(runtime):
    runtime.config.live_enabled = True
    runtime.pipeline._live_status_gate_decision = lambda _event: None
    runtime.bili_identity.resolve = AsyncMock(side_effect=AssertionError("no identity request"))
    runtime.viewer_store.record_live_danmaku = AsyncMock(side_effect=AssertionError("no disk write"))
    guard = runtime.safety_guard
    for _ in range(5):
        assert guard.before_event(event()).allowed
        guard.after_event()
    result = await runtime.pipeline.handle_event(event())
    assert result.reason == "input.viewer_rate_limited"
    runtime.bili_identity.resolve.assert_not_awaited()
    runtime.viewer_store.record_live_danmaku.assert_not_awaited()
    assert guard.queue_size == 0
    assert guard.snapshot()["viewer_input_rejected"] == 1
    guard.set_connected(False)
    guard.set_connected(True)
    assert guard.before_event(event()).allowed
    guard.after_event()
