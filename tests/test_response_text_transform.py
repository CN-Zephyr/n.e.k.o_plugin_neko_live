"""Actual plugin shaping through the paired host's pre-publication boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from plugin.plugins.neko_live import NekoLivePlugin
from plugin.plugins.neko_live.adapters.response_text_transform import (
    response_transform_metadata,
    shape_generated_reply,
)
from plugin.plugins.neko_live.core.contracts import LiveConfig
from plugin.plugins.neko_live.core.live_reply_contract import DISPATCH_REPLY_CHAR_LIMITS, build_reply_metadata
from plugin.plugins.neko_live.core.permission_gate import PermissionGate
from plugin.plugins.neko_live.core.safety_guard import SafetyGuard


def _context(module="danmaku_response", *, runtime=None):
    metadata = build_reply_metadata(uid="42", live_mode="solo_stream", response_module_hint=module)
    metadata.update(danmaku_profile="normal_line", danmaku_viewer_nickname="小林", live_event_source="live_danmaku")
    return response_transform_metadata(metadata, runtime=runtime)["response_text_transform"]["context"]


def _plugin():
    plugin = object.__new__(NekoLivePlugin)
    config = LiveConfig(live_enabled=True)
    plugin.runtime = SimpleNamespace(
        _stopping=False, _response_transform_epoch="synthetic-runtime", config=config,
        _live_session_generation=1, _live_listener_operation_id=1,
        safety_guard=SafetyGuard(config, Mock()), permission_gate=PermissionGate(config),
    )
    return plugin


@pytest.mark.parametrize("module", list(DISPATCH_REPLY_CHAR_LIMITS))
def test_shaping_boundary_enforces_route_ceiling_and_strips_stage_directions(module):
    result = shape_generated_reply("（对着镜头挥爪）今天的雨声很适合读书。后面这句话不该念出来。" * 5, _context(module))
    assert 0 < len(result["text"]) <= DISPATCH_REPLY_CHAR_LIMITS[module]
    assert "挥爪" not in result["text"]
    if module == "danmaku_response":
        assert result["text"].startswith("小林，")
        assert "后面" not in result["text"]


@pytest.mark.parametrize("bad", [{"neko_live_reply_shaped": True}, {"max_reply_chars": True},
                                 {"response_module_hint": "unknown"}, {"danmaku_profile": {}},
                                 {"danmaku_viewer_nickname": "x" * 513}])
def test_transform_rejects_bypass_flags_and_invalid_context(bad):
    with pytest.raises(ValueError, match="^output_policy.transform_failed$"):
        shape_generated_reply("reply", {**_context(), **bad})


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["stopped", "paused", "auto_paused", "developer_disabled"])
async def test_reserved_entry_rechecks_runtime_before_returning(condition):
    plugin = _plugin()
    plugin.runtime._stopping = condition == "stopped"
    plugin.runtime.safety_guard.manual_paused = condition == "paused"
    plugin.runtime.safety_guard.auto_paused = condition == "auto_paused"
    context = _context(runtime=plugin.runtime)
    context["demo"] = condition == "developer_disabled"
    with pytest.raises(ValueError, match="^output_policy.transform_failed$"):
        await plugin.__response_text_transform__("hello", context)


@pytest.mark.asyncio
async def test_clearing_queue_invalidates_already_submitted_transform():
    plugin = _plugin()
    context = _context(runtime=plugin.runtime)
    plugin.runtime.safety_guard.clear_queue()
    with pytest.raises(ValueError, match="^output_policy.transform_failed$"):
        await plugin.__response_text_transform__("hello", context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["resume", "session", "listener", "restart", "disabled", "disconnected", "dry_run", "missing_epoch"])
async def test_changed_delivery_state_rejects_old_result(change):
    plugin = _plugin()
    runtime = plugin.runtime
    context = _context(runtime=runtime)
    if change == "resume":
        runtime.safety_guard.pause()
        runtime.safety_guard.resume()
    elif change == "session":
        runtime._live_session_generation += 1
    elif change == "listener":
        runtime._live_listener_operation_id += 1
    elif change == "restart":
        runtime._response_transform_epoch = "replacement-runtime"
    elif change == "disabled":
        runtime.config.live_enabled = False
    elif change == "disconnected":
        runtime.safety_guard.set_connected(False)
    elif change == "dry_run":
        runtime.config.dry_run = True
    elif change == "missing_epoch":
        context.pop("delivery_epoch")
    with pytest.raises(ValueError, match="^output_policy.transform_failed$"):
        await plugin.__response_text_transform__("hello", context)


@pytest.mark.asyncio
async def test_new_submission_after_resume_works_without_charging_cooldown_twice():
    plugin = _plugin()
    plugin.runtime.safety_guard.resume()
    context = _context(runtime=plugin.runtime)
    plugin.runtime.safety_guard._last_output_at = 123.0
    assert await plugin.__response_text_transform__("今天很适合读书。", context)
    assert plugin.runtime.safety_guard._last_output_at == 123.0
