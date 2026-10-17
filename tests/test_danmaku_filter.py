from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.neko_live.core.contracts import LiveConfig, LiveEvent, ViewerEvent
from plugin.plugins.neko_live.core.event_bus import EventBus
from plugin.plugins.neko_live.core.pipeline import LivePipeline
from plugin.plugins.neko_live.core.runtime import LiveRuntime
from plugin.plugins.neko_live.modules.bili_live_ingest.livedanmaku import LiveDanmaku
from plugin.plugins.neko_live.modules.danmaku_filter import (
    IGNORED_PREFIX_REASON,
    DanmakuFilterModule,
    viewer_event_skip_reason,
)
from plugin.plugins.neko_live.modules.danmaku_filter.settings import normalize_prefixes
from plugin.plugins.neko_live.modules.live_events import LiveEventsModule
from plugin.plugins.neko_live.stores.audit_store import AuditStore


@pytest.mark.parametrize("text", ["!", "！", "!command", "！主播私聊", " \t\u3000！主播私聊"])
@pytest.mark.parametrize("kind", ["", "danmaku", "danmu", "chat"])
def test_prefix_opts_ordinary_chat_out(text, kind):
    assert DanmakuFilterModule().reason(text, event_type=kind) == IGNORED_PREFIX_REASON


@pytest.mark.parametrize("text", ["", "   ", "你好！", "你好!再见", "猫猫！在吗？"])
def test_other_text_is_not_filtered(text):
    assert DanmakuFilterModule().reason(text) == ""


@pytest.mark.parametrize("kind", ["gift", "sc", "super_chat", "guard", "entry", "like", "follow"])
def test_support_and_presence_keep_their_routes(kind):
    assert DanmakuFilterModule().reason("！支持你", event_type=kind) == ""


@pytest.mark.parametrize("source", ["idle_hosting", "active_engagement", "warmup_hosting"])
def test_filter_only_applies_to_viewer_input(source):
    assert viewer_event_skip_reason(SimpleNamespace(), ViewerEvent(uid="7", source=source, danmaku_text="!hello")) == ""


def _selection_context(mode):
    return SimpleNamespace(
        audit=AuditStore(), runtime_timeline=[], config=LiveConfig(live_mode=mode),
        event_bus=EventBus(), danmaku_filter=DanmakuFilterModule(), recent_results=[],
        safety_guard=SimpleNamespace(output_cooldown_remaining=lambda: 0.0),
        live_provider=SimpleNamespace(listener_state=lambda: {"viewer_count": 0}),
        live_target_lanlan="", handle_live_payload=AsyncMock(),
        dispatcher=SimpleNamespace(
            output_channel_status=lambda: {"ready": True}, push_ambient_room_context=AsyncMock(),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["solo_stream", "co_stream"])
@pytest.mark.parametrize("text", ["!主播私聊", "！主播私聊", " \t！主播私聊"])
@pytest.mark.parametrize("shape", ["envelope", "dict", "bilibili"])
async def test_filtered_chat_never_reaches_selection_context_or_passive_delivery(mode, text, shape):
    ctx = _selection_context(mode)
    hub = LiveEventsModule()
    await hub.setup(ctx)
    try:
        raw = None
        if shape == "dict":
            raw = {"event_type": "chat", "uid": "7", "nickname": "test-viewer", "text": text}
        elif shape == "bilibili":
            raw = LiveDanmaku.from_danmaku({"info": [[], text, [7, "test-viewer"], [], [0], 0, 0, 0]})
        event = LiveEvent(type="danmaku", uid="7", payload={"text": text}, raw=raw)
        ctx.event_bus.publish("danmaku", event)
        await asyncio.sleep(0)

        ctx.handle_live_payload.assert_not_called()
        ctx.dispatcher.push_ambient_room_context.assert_not_called()
        assert hub.recent_chat_snapshot(limit=3) == []
        assert hub._room_topic.status()["recent_danmaku_candidates"] == 0
        assert hub._recent_viewer_count() == 0
        assert hub._buffered_count == 0
        assert hub._last_dispatch_at == 0.0
        assert hub._tasks == set()
        assert hub._last_skip_reason == IGNORED_PREFIX_REASON
        assert ctx.runtime_timeline[-1]["reason"] == IGNORED_PREFIX_REASON
        assert text.strip() not in str(ctx.audit.recent())
        assert text.strip() not in str(ctx.runtime_timeline)
    finally:
        await hub.teardown()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["你好！", "猫猫!还在吗？"])
async def test_non_initial_exclamation_still_reaches_existing_selection(text):
    ctx = _selection_context("solo_stream")
    hub = LiveEventsModule()
    await hub.setup(ctx)
    try:
        hub.submit({"event_type": "danmaku", "uid": "7", "text": text})
        pending = tuple(hub._tasks)
        if pending:
            await asyncio.gather(*pending)
        ctx.handle_live_payload.assert_called_once()
        assert ctx.handle_live_payload.call_args.args[0]["danmaku_text"] == text
    finally:
        await hub.teardown()


@pytest.fixture
def runtime(tmp_path):
    plugin = SimpleNamespace(data_path=lambda: tmp_path, ctx=None, logger=None, config=None, push_message=Mock())
    return LiveRuntime(plugin)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["bilibili", "douyin", "twitch"])
@pytest.mark.parametrize("text", ["!不要接这句", "！不要接这句", " \t！不要接这句"])
async def test_real_normalizers_and_runtime_skip_before_activity_profiles_memory_or_output(runtime, platform, text):
    runtime.config.live_platform = platform
    runtime.config.viewer_memory_enabled = True
    runtime._hosting_without_viewer_count = 3
    runtime.viewer_profile.upsert = AsyncMock(side_effect=AssertionError("must not access profiles"))
    await runtime.live_events.setup(runtime)
    try:
        result = await runtime.handle_live_payload({
            "uid": "42", "nickname": "test-viewer", "danmaku_text": text, "event_type": "danmaku",
        })
        assert result.status == "skipped"
        assert result.reason == IGNORED_PREFIX_REASON
        assert result.request is None and result.identity is None
        assert runtime.live_events.recent_chat_snapshot(limit=3) == []
        assert runtime._last_live_danmaku_seen_at == 0.0
        assert runtime._hosting_without_viewer_count == 3
        assert runtime.safety_guard.queue_size == 0
        assert list(runtime.recent_results) == []
        assert getattr(runtime, "live_memory", None) is None
        runtime.viewer_profile.upsert.assert_not_called()
        runtime.plugin.push_message.assert_not_called()
        assert any(item["reason"] == IGNORED_PREFIX_REASON for item in runtime.runtime_timeline)
        assert text.strip() not in str(runtime.audit.recent())
        assert text.strip() not in str(runtime.runtime_timeline)
    finally:
        await runtime.live_events.teardown()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["live_danmaku", "manual_live_simulation", "developer_sandbox"])
async def test_direct_pipeline_ingress_cannot_bypass_filter(source):
    ctx = SimpleNamespace(audit=AuditStore(), runtime_timeline=[], danmaku_filter=DanmakuFilterModule())
    result = await LivePipeline(ctx).handle_event(ViewerEvent(uid="7", source=source, danmaku_text="！忽略"))
    assert result.status == "skipped" and result.reason == IGNORED_PREFIX_REASON
    assert result.request is None and result.identity is None
    assert result.steps[-1].id == "input_filter"


@pytest.mark.asyncio
async def test_registered_module_disable_and_enable_restore_the_expected_behavior(runtime):
    assert runtime.registry.get("danmaku_filter") is runtime.danmaku_filter
    event = ViewerEvent(uid="7", source="live_danmaku", danmaku_text="！忽略")
    assert viewer_event_skip_reason(runtime, event) == IGNORED_PREFIX_REASON
    assert await runtime.registry.disable("danmaku_filter", runtime)
    assert viewer_event_skip_reason(runtime, event) == ""
    assert await runtime.registry.enable("danmaku_filter", runtime)
    assert viewer_event_skip_reason(runtime, event) == IGNORED_PREFIX_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["handle_manual_event", "handle_sandbox_target"])
async def test_real_developer_actions_skip_without_populating_context(runtime, action):
    runtime.config.developer_tools_enabled = True
    await runtime.live_events.setup(runtime)
    try:
        result = await getattr(runtime, action)(uid="7", danmaku_text="！忽略", target_lanlan="TestCat")
        assert result.status == "skipped" and result.reason == IGNORED_PREFIX_REASON
        assert runtime.live_events.recent_chat_snapshot(limit=3) == []
        assert list(runtime.recent_results) == []
        assert list(runtime.recent_sandbox_results) == []
        runtime.plugin.push_message.assert_not_called()
    finally:
        await runtime.live_events.teardown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["handle_manual_event", "handle_sandbox_target"])
async def test_prefix_does_not_bypass_developer_mode_gate(runtime, action):
    with pytest.raises(PermissionError, match="developer mode is disabled"):
        await getattr(runtime, action)(uid="7", danmaku_text="！忽略")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gift", "sc", "super_chat", "guard", "entry", "like", "follow"])
async def test_support_and_presence_continue_past_pipeline_filter(kind):
    permission = Mock(return_value=(False, "test.downstream"))
    ctx = SimpleNamespace(
        audit=AuditStore(), runtime_timeline=[], danmaku_filter=DanmakuFilterModule(),
        permission_gate=SimpleNamespace(allows_source=permission),
    )
    result = await LivePipeline(ctx).handle_event(ViewerEvent(
        uid="7", source="live_danmaku", danmaku_text="！支持你", raw={"event_type": kind},
    ))
    assert result.reason == "test.downstream"
    permission.assert_called_once_with("live_danmaku")


@pytest.mark.parametrize("value", [" # \n私聊:\n#\n", [" # ", "私聊:", "#", ""]])
def test_custom_prefixes_normalize_and_round_trip_through_public_config(value):
    config = LiveConfig.from_mapping({"danmaku_filter_prefixes": value, "danmaku_filter_enabled": False})
    assert config.danmaku_filter_prefixes == "#\n私聊:"
    assert LiveConfig.from_mapping(config.to_dict()) == config
    assert config.to_public_dict()["danmaku_filter_prefixes"] == "#\n私聊:"
    assert config.to_public_dict()["danmaku_filter_enabled"] is False


@pytest.mark.parametrize("space", ["  ", "\t", "\u3000", "\u00a0", "\x85", "\v", "\u2028"])
@pytest.mark.parametrize("as_list", [False, True])
def test_prefix_whitespace_normalizes_without_splitting_or_broadening_rules(space, as_list):
    prefix = f"private{space}chat:"
    value = [prefix, "private chat:"] if as_list else f"{prefix}\nprivate chat:"
    config = LiveConfig.from_mapping({"danmaku_filter_prefixes": value})
    assert normalize_prefixes(value, strict=True) == "private chat:"
    assert config.danmaku_filter_prefixes == "private chat:"
    assert LiveConfig.from_mapping(config.to_dict()) == config
    module = DanmakuFilterModule()
    assert module.reason(f"{prefix} hidden", config=config) == IGNORED_PREFIX_REASON
    assert module.reason("private chat: hidden", config=config) == IGNORED_PREFIX_REASON
    assert module.reason("private unrelated", config=config) == ""
    assert module.reason("chat: unrelated", config=config) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["bilibili", "douyin", "twitch"])
@pytest.mark.parametrize("space", ["  ", "\t", "\u3000"])
async def test_custom_whitespace_prefix_survives_save_reload_and_real_ingress(runtime, platform, space):
    stored = {}

    async def save(payload):
        stored.update(payload["neko_live"])

    runtime.plugin.config = SimpleNamespace(
        update=AsyncMock(side_effect=save), dump=AsyncMock(return_value={"neko_live": stored}),
    )
    await runtime.update_config({"danmaku_filter_prefixes": f"private{space}chat:"})
    assert stored["danmaku_filter_prefixes"] == "private chat:"
    await runtime.reload_config()
    runtime.config.live_platform = platform
    await runtime.live_events.setup(runtime)
    try:
        text = f"private{space}chat: hidden"
        assert runtime.live_events.observe_danmaku({"event_type": "danmaku", "uid": "42", "text": text}) == 0
        result = await runtime.handle_live_payload({"event_type": "danmaku", "uid": "42", "danmaku_text": text})
        assert result.reason == IGNORED_PREFIX_REASON
        assert runtime.live_events.recent_chat_snapshot(limit=3) == []
        assert runtime._last_live_danmaku_seen_at == 0.0
        assert list(runtime.recent_results) == []
        runtime.plugin.push_message.assert_not_called()
        for source in ("manual_live_simulation", "developer_sandbox"):
            result = await runtime.pipeline.handle_event(ViewerEvent(uid="42", source=source, danmaku_text=text))
            assert result.reason == IGNORED_PREFIX_REASON
    finally:
        await runtime.live_events.teardown()


def test_empty_prefix_list_filters_nothing_and_old_profiles_keep_defaults():
    module = DanmakuFilterModule()
    empty = LiveConfig.from_mapping({"danmaku_filter_prefixes": " \n\t"})
    assert empty.danmaku_filter_prefixes == ""
    assert module.reason("！聊天", config=empty) == ""
    assert module.reason("!聊天", config=LiveConfig.from_mapping({})) == IGNORED_PREFIX_REASON


def test_custom_prefixes_replace_defaults_and_are_literal_case_sensitive_and_hot_applied():
    module = DanmakuFilterModule()
    config = LiveConfig(danmaku_filter_prefixes="#\n私聊:\nFOO\n.")
    for text in (" #聊天", "私聊:你好", "FOObar", ".聊天"):
        assert module.reason(text, config=config) == IGNORED_PREFIX_REASON
    for text in ("!聊天", "！聊天", "foo", "x聊天", "聊天#"):
        assert module.reason(text, config=config) == ""
    config.danmaku_filter_prefixes = "@"
    assert module.reason("#聊天", config=config) == ""
    assert module.reason("@聊天", config=config) == IGNORED_PREFIX_REASON
    config.danmaku_filter_enabled = False
    assert module.reason("@聊天", config=config) == ""
    config.danmaku_filter_enabled = True
    assert module.reason("@聊天", config=config) == IGNORED_PREFIX_REASON


@pytest.mark.parametrize("value", [None, 42, [42], "x" * 65, "\n".join(str(i) for i in range(33)), " " * 8193])
def test_invalid_or_oversized_updates_are_rejected_without_saving(runtime, value):
    from plugin.plugins.neko_live.core.runtime_config_activation import clean_config_updates

    with pytest.raises(ValueError, match="danmaku_filter"):
        clean_config_updates({"danmaku_filter_prefixes": value})
    assert runtime.config.danmaku_filter_prefixes == "!\n！"
    runtime.plugin.push_message.assert_not_called()


def test_prefix_bounds_do_not_shorten_a_rule_into_a_broader_match():
    assert normalize_prefixes("x" * 65) == ""
    assert normalize_prefixes("🙂" * 64, strict=True) == "🙂" * 64
    assert len(normalize_prefixes("\n".join(str(i) for i in range(33))).splitlines()) == 32


@pytest.mark.asyncio
async def test_settings_are_persisted_reloaded_and_immediately_used_by_all_ingress(runtime):
    stored = {}

    async def save(payload):
        stored.update(payload["neko_live"])

    runtime.plugin.config = SimpleNamespace(update=AsyncMock(side_effect=save), dump=AsyncMock(return_value={"neko_live": stored}))
    await runtime.update_config({"danmaku_filter_enabled": True, "danmaku_filter_prefixes": " # \n私聊:\n#"})
    assert stored["danmaku_filter_prefixes"] == "#\n私聊:"
    await runtime.reload_config()
    assert runtime.config.danmaku_filter_prefixes == "#\n私聊:"
    await runtime.live_events.setup(runtime)
    try:
        result = await runtime.handle_live_payload({"uid": "42", "danmaku_text": "#不接这句", "event_type": "danmaku"})
        assert result.reason == IGNORED_PREFIX_REASON
        assert runtime.live_events.observe_danmaku({"event_type": "danmaku", "uid": "42", "text": "私聊:不接这句"}) == 0
        assert runtime.live_events.recent_chat_snapshot(limit=3) == []
        assert "私聊:" not in str(runtime.audit.recent())
        runtime.plugin.push_message.assert_not_called()
    finally:
        await runtime.live_events.teardown()
