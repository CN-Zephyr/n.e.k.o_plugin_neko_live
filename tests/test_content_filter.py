from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.neko_live.core.contracts import (
    InteractionRequest,
    LiveConfig,
    ViewerEvent,
    ViewerIdentity,
    ViewerProfile,
)
from plugin.plugins.neko_live.core.pipeline_dispatch import dispatch_routed_request
from plugin.plugins.neko_live.core.runtime_config_activation import clean_config_updates
from plugin.plugins.neko_live.modules.content_filter import response_policy_metadata, skip_reason
from plugin.plugins.neko_live.modules.content_filter.settings import normalize_terms
from plugin.plugins.neko_live.modules.danmaku_filter import viewer_event_skip_reason
from plugin.plugins.neko_live.modules.live_events import LiveEventsModule


@pytest.mark.parametrize("text", ["BAD", "ｂａｄ", "b\u200bad", "prefixbadpostfix"])
def test_literal_policy_normalizes_case_width_and_format_characters(text):
    assert skip_reason(LiveConfig(content_filter_terms="bad"), text) == "content.blocked_term"


def test_literal_policy_is_optional_and_does_not_interpret_regex():
    assert normalize_terms(" BAD \nbad\n\nＡＢＣ") == "bad\nabc"
    assert skip_reason(LiveConfig(), "bad") == ""
    config = LiveConfig(content_filter_terms="a.*b")
    assert skip_reason(config, "axb") == ""
    assert skip_reason(config, "a.*b") == "content.blocked_term"
    assert skip_reason(config, "x" * 32769) == "content.input_too_large"
    config.content_filter_terms = "different"
    assert skip_reason(config, "a.*b") == ""


@pytest.mark.parametrize("value", [None, [], "x" * 65, "x" * 8193, "\n".join(str(i) for i in range(129))])
def test_invalid_setting_rejected_before_persistence(value):
    with pytest.raises(ValueError, match="content_filter.invalid_terms"):
        clean_config_updates({"content_filter_terms": value})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["nickname", "danmaku_text"])
async def test_input_filter_precedes_identity_profile_and_recent_chat(runtime, field):
    runtime.config.content_filter_terms = "blockedsecret"
    runtime.config.danmaku_filter_enabled = False
    runtime.bili_identity.resolve = AsyncMock(side_effect=AssertionError("no identity lookup"))
    runtime.viewer_store.record_live_danmaku = AsyncMock(side_effect=AssertionError("no profile write"))
    event = ViewerEvent(uid="7", nickname="viewer", source="live_danmaku", danmaku_text="hello")
    setattr(event, field, "blockedsecret")
    assert viewer_event_skip_reason(runtime, event) == "content.blocked_term"
    result = await runtime.pipeline.handle_event(event)
    assert result.reason == "content.blocked_term"
    runtime.bili_identity.resolve.assert_not_awaited()
    runtime.viewer_store.record_live_danmaku.assert_not_awaited()
    live = LiveEventsModule()
    live.ctx = runtime
    assert live.observe_danmaku(event) == 0
    assert not runtime.plugin.pushed_messages


def request(text="hello"):
    return InteractionRequest(
        event=ViewerEvent(uid="7", source="developer_sandbox", target_lanlan="Cat", danmaku_text=text),
        identity=ViewerIdentity(uid="7", nickname="viewer"), profile=ViewerProfile(uid="7", nickname="viewer"),
        prompt_text=text, live_mode="co_stream", strength="normal",
    )


@pytest.mark.asyncio
async def test_setting_change_during_host_read_blocks_before_output_cooldown(runtime):
    runtime.config.developer_tools_enabled = True
    req = request("blockedsecret")

    async def prepare(_request):
        runtime.config.content_filter_terms = "blockedsecret"

    runtime.dispatcher.prepare_recent_replies = prepare
    runtime.safety_guard.before_output = Mock(side_effect=AssertionError("no output cooldown consumed"))
    result = await dispatch_routed_request(
        runtime, SimpleNamespace(), event=req.event, identity=req.identity, profile=req.profile, request=req,
        steps=[], response_module_id="danmaku_response", should_mark_roasted=False, mark_avatar_roast_sent=lambda: None,
    )
    assert result.reason == "content.blocked_term"
    runtime.safety_guard.before_output.assert_not_called()
    assert not runtime.plugin.pushed_messages


@pytest.mark.asyncio
async def test_dispatcher_checks_added_host_context_and_all_text_delivery_paths(runtime):
    runtime.config.content_filter_terms = "NEKO Live short output contract"
    req = request()
    assert skip_reason(runtime.config, req.prompt_text) == ""
    assert "content.blocked_term" in await runtime.dispatcher.push_roast(req)
    assert not req.should_push
    runtime.config.content_filter_terms = "blockedsecret"
    assert "content.blocked_term" in await runtime.dispatcher.push_ambient_room_context("blockedsecret", session_key="one")
    assert "content.blocked_term" in await runtime.dispatcher.push_developer_announcement("blockedsecret")
    assert not runtime.plugin.pushed_messages
    # The tombstone must still replace an already delivered passive snapshot.
    await runtime.dispatcher.push_ambient_room_context("blockedsecret", session_key="one", expired=True)
    assert runtime.plugin.pushed_messages[-1]["metadata"]["context_expired"] is True


@pytest.mark.asyncio
async def test_generated_text_policy_is_scoped_to_respond_transport_and_frozen(runtime):
    runtime.config.content_filter_terms = "ＦＯＲＢＩＤ\nforbid"
    req = request()
    await runtime.dispatcher.push_roast(req)
    payload = runtime.plugin.pushed_messages[-1]
    assert payload["ai_behavior"] == "respond"
    assert payload["metadata"]["response_text_policy"] == {"version": 1, "blocked_terms": ["forbid"]}
    assert "response_text_policy" not in req.metadata
    runtime.config.content_filter_terms = "changed"
    assert payload["metadata"]["response_text_policy"]["blocked_terms"] == ["forbid"]
    await runtime.dispatcher.push_developer_announcement("hello")
    assert runtime.plugin.pushed_messages[-1]["metadata"]["response_text_policy"]["blocked_terms"] == ["changed"]
    await runtime.dispatcher.push_ambient_room_context("hello", session_key="one")
    assert "response_text_policy" not in runtime.plugin.pushed_messages[-1]["metadata"]
    runtime.config.content_filter_terms = ""
    assert response_policy_metadata(runtime.config) == {}
    await runtime.dispatcher.push_roast(request())
    assert "response_text_policy" not in runtime.plugin.pushed_messages[-1]["metadata"]
