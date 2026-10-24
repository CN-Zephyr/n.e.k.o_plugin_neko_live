from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from plugin.plugins.neko_live.adapters.neko_dispatcher import NekoDispatcher
from plugin.plugins.neko_live.adapters.output_contract_bridge import response_module_hint
from plugin.plugins.neko_live.core.contracts import (
    LiveConfig,
    SafetyDecision,
    ViewerEvent,
    ViewerIdentity,
    ViewerProfile,
)
from plugin.plugins.neko_live.core.permission_gate import PermissionGate
from plugin.plugins.neko_live.core.pipeline import LivePipeline
from plugin.plugins.neko_live.core.runtime_live_controls import clear_queue
from plugin.plugins.neko_live.core.safety_guard import SafetyGuard
from plugin.plugins.neko_live.modules.avatar_roast import AvatarRoastModule
from plugin.plugins.neko_live.modules.bili_identity import BiliIdentityModule
from plugin.plugins.neko_live.modules.danmaku_response import DanmakuResponseModule
from plugin.plugins.neko_live.modules.danmaku_response.avatar_request import requests_own_avatar
from plugin.plugins.neko_live.modules.live_support_events import LiveSupportEventsModule
from plugin.plugins.neko_live.stores.audit_store import AuditStore
from utils.tokenize import count_tokens


def avatar_png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def assert_avatar_color(data, color):
    # Dispatcher may re-encode PNG to JPEG. Check visual identity, not bytes.
    with Image.open(io.BytesIO(data)) as image:
        actual = image.convert("RGB").getpixel((0, 0))
    expected = Image.new("RGB", (1, 1), color).getpixel((0, 0))
    assert all(abs(a - b) <= 4 for a, b in zip(actual, expected))


def assert_attributed(text, name, utterance):
    assert re.search(r"【" + re.escape(name) + r"·v_[0-9a-f]{12}】" + re.escape(utterance), text)


@pytest.fixture
def live_context():
    config = LiveConfig(live_enabled=True, entry_roast_enabled=True, entry_greet_enabled=True, rate_limit_seconds=0)
    roasted = set()

    async def resolve(event):
        return ViewerIdentity(uid=event.uid, nickname=event.nickname)

    async def upsert(identity):
        return ViewerProfile(uid=identity.uid, nickname=identity.nickname, roast_count=int(identity.uid in roasted))

    async def mark(uid, _output):
        roasted.add(uid)
        return True

    messages = []
    plugin = SimpleNamespace(ctx=SimpleNamespace(_current_lanlan="CatA"), push_message=lambda **kw: messages.append(kw))
    ctx = SimpleNamespace(
        config=config, audit=AuditStore(), runtime_timeline=[], recent_results=[],
        permission_gate=PermissionGate(config),
        safety_guard=SimpleNamespace(
            before_event=lambda _: SafetyDecision(True), before_output=lambda _: SafetyDecision(True),
            after_event=lambda: None, record_failure=lambda *_: None,
        ),
        bili_identity=SimpleNamespace(resolve=resolve),
        viewer_profile=SimpleNamespace(
            upsert=upsert, has_roasted=AsyncMock(side_effect=lambda uid: uid in roasted),
            mark_roasted=AsyncMock(side_effect=mark),
        ),
        avatar_roast=AvatarRoastModule(), danmaku_response=DanmakuResponseModule(),
        dispatcher=NekoDispatcher(plugin), messages=messages, roasted=roasted,
    )
    ctx.record_result = ctx.recent_results.append
    ctx.avatar_roast.ctx = ctx.danmaku_response.ctx = ctx
    return ctx


def entry(uid="42", action="roast", mode="solo_stream"):
    return ViewerEvent(uid=uid, nickname="小鱼", source="live_danmaku", live_mode=mode,
                       raw={"event_type": "entry", "presence_action": action})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["solo_stream", "co_stream"])
async def test_returning_viewer_entry_falls_back_to_welcome_under_uid_lock(live_context, mode):
    ctx = live_context
    ctx.roasted.add("42")
    result = await LivePipeline(ctx).handle_event(entry(mode=mode))
    assert result.status == "pushed"
    assert result.request.metadata["danmaku_profile"] == "entry_greeting"
    assert result.request.event.raw["presence_action"] == "greet"
    assert response_module_hint(result.request) == "danmaku_response"
    assert "Welcome the arriving viewer" in ctx.messages[-1]["parts"][0]["text"]
    ctx.viewer_profile.mark_roasted.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_first_entries_send_one_roast_then_one_welcome(live_context):
    ctx = live_context
    pipeline = LivePipeline(ctx)
    results = await asyncio.gather(pipeline.handle_event(entry()), pipeline.handle_event(entry()))
    assert [result.status for result in results] == ["pushed", "pushed"]
    assert [response_module_hint(result.request) for result in results] == ["avatar_roast", "danmaku_response"]
    ctx.viewer_profile.mark_roasted.assert_awaited_once()
    assert pipeline.session._uid_locks == {}


@pytest.mark.asyncio
async def test_disabled_welcome_keeps_returning_entry_silent(live_context):
    ctx = live_context
    ctx.config.entry_greet_enabled = False
    ctx.roasted.add("42")
    result = await LivePipeline(ctx).handle_event(entry())
    assert result.status == "skipped"
    assert not ctx.messages


@pytest.mark.asyncio
async def test_greeting_alone_does_not_consume_first_chat_roast(live_context):
    ctx = live_context
    ctx.config.entry_roast_enabled = False
    pipeline = LivePipeline(ctx)
    greeting = await pipeline.handle_event(entry(action="greet"))
    chat = await pipeline.handle_event(ViewerEvent(uid="42", nickname="小鱼", source="live_danmaku", danmaku_text="你好"))
    assert greeting.status == chat.status == "pushed"
    assert response_module_hint(greeting.request) == "danmaku_response"
    assert response_module_hint(chat.request) == "avatar_roast"
    ctx.viewer_profile.mark_roasted.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["co_stream", "solo_stream"])
@pytest.mark.parametrize("enabled,visible", [(True, True), (False, True), (True, False)])
async def test_avatar_description_uses_current_viewer_image_or_explicit_unknown(live_context, enabled, visible, mode):
    ctx = live_context
    ctx.config.avatar_analysis_enabled = enabled
    module = ctx.danmaku_response
    image_a, image_b = avatar_png("red"), avatar_png("blue")
    identity_a = ViewerIdentity(uid="A", nickname="法拉利", avatar_bytes=image_a)
    identity_b = ViewerIdentity(uid="B", nickname="小鱼", avatar_bytes=image_b if visible else None)
    event_a = ViewerEvent(uid="A", nickname="法拉利", danmaku_text="你好", source="live_danmaku", live_mode=mode)
    event_b = ViewerEvent(uid="B", nickname="小鱼", danmaku_text="描述我的头像", source="live_danmaku", live_mode=mode)
    await ctx.dispatcher.push_roast(ctx.avatar_roast.build_request(event_a, identity_a, ViewerProfile(uid="A", nickname="法拉利")))
    request = module.build_request(event_b, identity_b, ViewerProfile(uid="B", nickname="小鱼"))
    await ctx.dispatcher.push_roast(request)
    message = ctx.messages[-1]
    assert response_module_hint(request) == message["metadata"]["response_module_hint"] == "danmaku_response"
    images = [part["data"] for part in message["parts"] if part["type"] == "image"]
    if enabled and visible:
        assert len(images) == 1
        assert_avatar_color(images[0], "blue")
    else:
        assert images == []
    text = message["parts"][0]["text"]
    assert "Never reuse historical images" in text
    assert_attributed(text, "小鱼", "描述我的头像")
    assert "do not mention avatar, ID" not in text
    assert "current_avatar_question" in text
    assert count_tokens(text) <= 1000


@pytest.mark.asyncio
async def test_ordinary_reply_keeps_current_author_inline_without_extra_image(live_context):
    ctx = live_context
    event = ViewerEvent(uid="A", nickname="法拉利", danmaku_text="天依肯定先救乐正绫", source="live_danmaku")
    identity = ViewerIdentity(uid="A", nickname="法拉利", avatar_bytes=b"image-A")
    request = ctx.danmaku_response.build_request(event, identity, ViewerProfile(uid="A", nickname="法拉利"))
    await ctx.dispatcher.push_roast(request)
    parts = ctx.messages[-1]["parts"]
    assert len(parts) == 1
    assert_attributed(parts[0]["text"], "法拉利", "天依肯定先救乐正绫")


@pytest.mark.asyncio
async def test_preferred_reply_name_does_not_replace_public_speaker_identity(live_context):
    ctx = live_context
    event = ViewerEvent(uid="42", nickname="小鱼0510", danmaku_text="你好", source="live_danmaku")
    request = ctx.danmaku_response.build_request(
        event, ViewerIdentity(uid="42", nickname="小鱼0510"), ViewerProfile(uid="42", nickname="小鱼0510"),
    )
    request.metadata["danmaku_viewer_nickname"] = "小鱼"
    await ctx.dispatcher.push_roast(request)
    message = ctx.messages[-1]
    assert message["metadata"]["danmaku_viewer_nickname"] == "小鱼"
    assert_attributed(message["parts"][0]["text"], "小鱼0510", "你好")
    assert "danmaku_author: 小鱼0510" in message["parts"][0]["text"]


@pytest.mark.asyncio
async def test_history_question_has_no_invented_first_meeting_evidence(live_context):
    ctx = live_context
    event = ViewerEvent(uid="B", nickname="Baka", danmaku_text="我们是什么时候相见的", source="live_danmaku")
    request = ctx.danmaku_response.build_request(event, ViewerIdentity(uid="B", nickname="Baka"), ViewerProfile(uid="B", nickname="Baka"))
    await ctx.dispatcher.push_roast(request)
    text = ctx.messages[-1]["parts"][0]["text"]
    assert "Never invent first visits or meeting dates" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["describe my profile picture", "how do I look?", "我的脸长啥样", "描述我的頭像"])
async def test_missing_image_status_follows_avatar_task_across_wording(live_context, question):
    ctx = live_context
    event = ViewerEvent(uid="42", nickname="viewer", danmaku_text=question, source="live_danmaku")
    request = ctx.danmaku_response.build_request(
        event, ViewerIdentity(uid="42", nickname="viewer"), ViewerProfile(uid="42", nickname="viewer"),
    )
    result = await ctx.dispatcher.push_roast(request)
    assert result.startswith("queued_to_neko(")
    text = ctx.messages[-1]["parts"][0]["text"]
    assert "Current viewer avatar: unavailable in this request" in text
    assert "Never reuse historical images" in text
    assert count_tokens(text) <= 1000


@pytest.mark.parametrize("text", ["描述我的头像", "看看我头像", "描述我的頭像", "describe my avatar", "how do I look?"])
def test_current_avatar_requests_work_with_roast_disabled(live_context, text):
    ctx = live_context
    ctx.config.avatar_roast_enabled = False
    identity_provider = BiliIdentityModule()
    identity_provider.ctx = ctx
    event = ViewerEvent(uid="42", nickname="小鱼", source="live_danmaku", danmaku_text=text)
    assert identity_provider._should_fetch_avatar_image(event, requested=True)
    assert not identity_provider._should_fetch_avatar_image(event, requested=False)
    ctx.config.avatar_analysis_enabled = False
    assert not identity_provider._should_fetch_avatar_image(event, requested=True)


@pytest.mark.parametrize("text", ["描述他的头像", "小鱼的头像是什么", "他说“描述我的头像”", "hi", "why?"])
def test_ordinary_or_quoted_requests_do_not_attach_current_viewer(text):
    assert not requests_own_avatar(text)


@pytest.mark.asyncio
async def test_alternating_viewers_pipeline_binds_each_image_without_rerouting_to_roast(live_context):
    ctx = live_context
    ctx.config.entry_roast_enabled = False
    colors = {"1": avatar_png("red"), "2": avatar_png("blue")}

    async def resolve(event):
        return ViewerIdentity(uid=event.uid, nickname=event.nickname, avatar_bytes=colors[event.uid])

    ctx.bili_identity.resolve = resolve
    ctx.roasted.add("2")
    pipeline = LivePipeline(ctx)
    for uid, text in [("1", "你好"), ("2", "描述我的头像"), ("1", "描述我的头像")]:
        result = await pipeline.handle_event(ViewerEvent(uid=uid, nickname=f"viewer{uid}", source="live_danmaku", danmaku_text=text))
        assert result.status == "pushed"
        message = ctx.messages[-1]
        assert_avatar_color(message["parts"][1]["data"], "red" if uid == "1" else "blue")
        assert_attributed(message["parts"][0]["text"], f"viewer{uid}", text)
    assert [message["metadata"]["response_module_hint"] for message in ctx.messages] == [
        "avatar_roast", "danmaku_response", "danmaku_response",
    ]
    ctx.viewer_profile.mark_roasted.assert_awaited_once()


@pytest.mark.asyncio
async def test_oversized_avatar_and_oversized_optional_context_keep_missing_image_boundary(live_context):
    ctx = live_context
    event = ViewerEvent(uid="42", nickname="小鱼", source="live_danmaku", danmaku_text="描述我的头像")
    identity = ViewerIdentity(uid="42", nickname="小鱼", avatar_bytes=b"x" * (64 * 1024 + 1))
    request = ctx.danmaku_response.build_request(event, identity, ViewerProfile(uid="42", nickname="小鱼"))
    request.prompt_text = request.prompt_text.replace("\nRules:\n", "\nRecent spent live material:\n" + "old talk " * 800 + "\n\nRules:\n")
    result = await ctx.dispatcher.push_roast(request)
    assert result.startswith("queued_to_neko(")
    parts = ctx.messages[-1]["parts"]
    assert len(parts) == 1
    assert "Current viewer avatar: unavailable in this request" in parts[0]["text"]
    assert "Never reuse historical images" in parts[0]["text"]
    assert count_tokens(parts[0]["text"]) <= 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["co_stream", "solo_stream"])
@pytest.mark.parametrize("avatar_on,entry_on,greet_on,expected", [
    (False, True, True, "danmaku_response"), (False, True, False, None),
    (True, False, True, "danmaku_response"), (True, False, False, None),
    (True, True, True, "avatar_roast"),
])
async def test_entry_uses_effective_master_and_presence_switches(live_context, mode, avatar_on, entry_on, greet_on, expected):
    ctx = live_context
    ctx.config.avatar_roast_enabled = avatar_on
    ctx.config.entry_roast_enabled = entry_on
    ctx.config.entry_greet_enabled = greet_on
    result = await LivePipeline(ctx).handle_event(entry(mode=mode))
    if expected is None:
        assert result.status == "skipped" and result.reason == "live_presence.disabled"
        assert not ctx.messages
        assert ctx.runtime_timeline[-1]["reason"] == "live_presence.disabled"
    else:
        assert result.status == "pushed"
        assert response_module_hint(result.request) == expected
        if expected == "danmaku_response":
            assert result.request.metadata["danmaku_profile"] == "entry_greeting"
            ctx.viewer_profile.mark_roasted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["greet", "roast"])
@pytest.mark.parametrize("waiting_at", ["identity", "roast_history"])
async def test_inflight_entry_checks_current_switches_after_await(live_context, action, waiting_at):
    ctx = live_context
    started, release = asyncio.Event(), asyncio.Event()

    async def resolve(event):
        started.set()
        await release.wait()
        return ViewerIdentity(uid=event.uid, nickname=event.nickname)

    async def has_roasted(_uid):
        started.set()
        await release.wait()
        return False

    if waiting_at == "identity":
        ctx.bili_identity.resolve = resolve
    else:
        ctx.viewer_profile.has_roasted = AsyncMock(side_effect=has_roasted)
    task = asyncio.create_task(LivePipeline(ctx).handle_event(entry(action=action)))
    try:
        await asyncio.wait_for(started.wait(), 1)
        ctx.config = LiveConfig.from_mapping({**ctx.config.to_dict(), "entry_greet_enabled": False, "entry_roast_enabled": False})
    finally:
        release.set()
    result = await asyncio.wait_for(task, 1)
    assert result.status == "skipped" and result.reason == "live_presence.disabled"
    assert not ctx.messages
    ctx.viewer_profile.mark_roasted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("roasted", [False, True])
@pytest.mark.parametrize("text", [
    "别再评价我的头像了，回答刚才的问题", "我不是让你描述我的头像", "我的头像不用看了",
    "Don't review my avatar", "Don't roast my avatar", "Stop roasting my avatar",
    "I am not asking you to describe my avatar", "leave my avatar alone",
])
async def test_declining_avatar_never_fetches_attaches_or_enters_roast(live_context, roasted, text):
    ctx = live_context
    if roasted:
        ctx.roasted.add("42")
    provider = BiliIdentityModule()
    provider.ctx = ctx
    event = ViewerEvent(uid="42", nickname="viewer", source="live_danmaku", danmaku_text=text)
    assert not provider._should_fetch_avatar_image(event, requested=True)

    async def resolve(event):
        return ViewerIdentity(uid=event.uid, nickname=event.nickname, avatar_bytes=avatar_png("blue"))

    ctx.bili_identity.resolve = resolve
    result = await LivePipeline(ctx).handle_event(event)
    assert result.status == "pushed"
    assert response_module_hint(result.request) == "danmaku_response"
    assert result.request.metadata["danmaku_profile"] != "current_avatar_question"
    assert not result.request.attach_avatar_image
    assert len(ctx.messages[-1]["parts"]) == 1
    assert any(step.message == "avatar_roast_declined" for step in result.steps)
    ctx.viewer_profile.mark_roasted.assert_not_called()


@pytest.mark.parametrize("text", [
    "他说 'describe my avatar'，你怎么看？", "他说‘锐评我的头像’", '他说 "review my avatar"',
    "He said 'I don't like my avatar'", "她说描述我的头像是什么意思",
])
def test_quoted_and_reported_self_avatar_request_is_not_current_viewer(text):
    assert not requests_own_avatar(text)


@pytest.mark.parametrize("text", ["我的头像不好看吗？", "别评价我的头像，只描述我的头像颜色", "Don't worry, describe my avatar"])
def test_negative_words_do_not_blanket_block_clear_current_requests(text):
    assert requests_own_avatar(text)


@pytest.mark.asyncio
async def test_author_tag_distinguishes_long_or_identical_names_and_survives_rename(live_context):
    ctx = live_context
    names = ["abcdefghijklmnopqrstuaaa1", "abcdefghijklmnopqrstubbb2"]
    tags = []
    for uid, name in [("twitch:1", names[0]), ("twitch:2", names[1]), ("twitch:1", "renamed"), ("twitch:2", "renamed")]:
        event = ViewerEvent(uid=uid, nickname=name, source="live_danmaku", danmaku_text="hello")
        request = ctx.danmaku_response.build_request(event, ViewerIdentity(uid=uid, nickname=name), ViewerProfile(uid=uid, nickname=name))
        await ctx.dispatcher.push_roast(request)
        text = ctx.messages[-1]["parts"][0]["text"]
        tags.append(re.search(r"danmaku: 【[^】]*·(v_[0-9a-f]{12})】hello", text)[1])
        assert "never speak this ID" in text
        assert count_tokens(text) <= 1000
    assert tags[0] != tags[1]
    assert tags[0] == tags[2]
    assert tags[1] == tags[3]
    assert tags[2] != tags[3]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["co_stream", "solo_stream"])
@pytest.mark.parametrize("name", [
    "测试观众喵喵喵喵喵喵喵喵喵喵", "鱼" * 20, "猫🐱" * 10,
    "龍龘靐齉齾爩鱻麤灪龖龗灩爨癵籲龘靐齉齾爩", "a" * 25, "鱼" * 30,
])
@pytest.mark.parametrize("text", ["你好", "描述我的头像", "你还记得我们第一次认识是什么时候吗？"])
@pytest.mark.parametrize("room_status", ["live", "unknown"])
async def test_first_roast_budget_with_chinese_emoji_and_history_requests(live_context, mode, name, text, room_status):
    ctx = live_context
    ctx.config.entry_roast_enabled = False
    ctx.dispatcher.runtime = SimpleNamespace(
        live_provider=SimpleNamespace(platform="bilibili", configured_room_ref=lambda: "42"),
        live_room_context={"platform": "bilibili", "room_ref": "42", "live_status": room_status},
    )

    async def resolve(event):
        return ViewerIdentity(uid=event.uid, nickname=event.nickname, avatar_bytes=avatar_png("red"))

    ctx.bili_identity.resolve = resolve
    result = await LivePipeline(ctx).handle_event(
        ViewerEvent(uid="42", nickname=name, source="live_danmaku", danmaku_text=text, live_mode=mode),
    )
    assert result.status == "pushed", result.reason
    assert response_module_hint(result.request) == "avatar_roast"
    delivered = ctx.messages[-1]["parts"][0]["text"]
    assert count_tokens(delivered) <= 1000
    assert "never seek permission to broadcast/interact" in delivered
    assert ("Broadcast live." in delivered) == (room_status == "live")
    assert "danmaku_author:" in delivered and "never speak this ID" in delivered
    assert "Never reuse historical images" in delivered
    assert "公开示众, 劳改, 审判, 处刑, 惩罚" in delivered
    if "第一次" in text:
        assert "Never invent first visits or meeting dates" in delivered
    ctx.viewer_profile.mark_roasted.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("resist_cancellation", [False, True])
async def test_clear_queue_stops_pending_pipeline_and_preserves_slot_until_exit(live_context, resist_cancellation):
    ctx = live_context
    ctx.config.queue_limit = 1
    ctx.roasted.add("42")
    ctx.safety_guard = SafetyGuard(ctx.config, ctx.audit)
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(event):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if not resist_cancellation:
                raise
            await release.wait()
        return ViewerIdentity(uid=event.uid, nickname=event.nickname)

    ctx.bili_identity.resolve = resolve
    pipeline = LivePipeline(ctx)
    event = ViewerEvent(uid="42", nickname="viewer", source="live_danmaku", danmaku_text="hello")
    pending = asyncio.create_task(pipeline.handle_event(event))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        clear_queue(ctx)
        assert ctx.safety_guard.queue_size == 1
        rejected = await pipeline.handle_event(event)
        assert rejected.status == "skipped"
        release.set()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        if resist_cancellation:
            assert result.status == "skipped" and result.reason == "queue.cleared"
        else:
            assert isinstance(result, asyncio.CancelledError)
        assert not ctx.messages
        assert ctx.safety_guard.queue_size == 0
        assert not ctx.safety_guard._event_tasks
        ctx.viewer_profile.mark_roasted.assert_not_called()
        fresh = await pipeline.handle_event(event)
        assert fresh.status == "pushed"
        assert ctx.safety_guard.queue_size == 0
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["like", "follow"])
@pytest.mark.parametrize("waiting_at", ["identity", "roast_history"])
async def test_presence_support_checks_latest_switch_after_lookup(live_context, kind, waiting_at):
    ctx = live_context
    setattr(ctx.config, kind + "_greet_enabled", True)
    ctx.live_support_events = LiveSupportEventsModule()
    ctx.live_support_events.ctx = ctx
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(event):
        entered.set()
        await release.wait()
        return ViewerIdentity(uid=event.uid, nickname=event.nickname)

    async def history(_uid):
        entered.set()
        await release.wait()
        return False

    if waiting_at == "identity":
        ctx.bili_identity.resolve = resolve
    else:
        ctx.viewer_profile.has_roasted = AsyncMock(side_effect=history)
    event = ViewerEvent(uid="42", nickname="viewer", source="live_danmaku", danmaku_text="点赞了5次",
                        raw={"event_type": kind, "support_verified": True, "gift_name": kind, "gift_num": 5})
    pending = asyncio.create_task(LivePipeline(ctx).handle_event(event))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        ctx.config = LiveConfig.from_mapping({**ctx.config.to_dict(), kind + "_greet_enabled": False})
    finally:
        release.set()
    result = await asyncio.wait_for(pending, 1)
    assert result.status == "skipped" and result.reason == kind + "_greet.disabled"
    assert not ctx.messages
    ctx.viewer_profile.mark_roasted.assert_not_called()


@pytest.mark.asyncio
async def test_history_question_cites_recorded_first_seen_date(live_context):
    ctx = live_context
    today = datetime.now().astimezone().date()
    prior = (today - timedelta(days=60)).isoformat()
    event = ViewerEvent(uid="B", nickname="Baka", danmaku_text="我们是什么时候第一次见面的", source="live_danmaku")
    profile = ViewerProfile(uid="B", nickname="Baka", first_seen_at=f"{prior}T12:00:00+00:00")
    identity = ViewerIdentity(uid="B", nickname="Baka")
    request = ctx.danmaku_response.build_request(event, identity, profile)
    request.profile = profile
    await ctx.dispatcher.push_roast(request)
    text = ctx.messages[-1]["parts"][0]["text"]
    assert prior in text and "first seen" in text.lower()
    assert "Never invent first visits or meeting dates" in text


@pytest.mark.asyncio
async def test_same_day_first_seen_is_not_cited_as_meeting_date(live_context):
    # A fresh or unreadable record is filled with "now"; citing it would let
    # the model claim "we met today" without evidence.
    ctx = live_context
    event = ViewerEvent(uid="B", nickname="Baka", danmaku_text="你还记得我们第一次认识是什么时候吗", source="live_danmaku")
    identity = ViewerIdentity(uid="B", nickname="Baka")
    profile = ViewerProfile(uid="B", nickname="Baka")
    request = ctx.danmaku_response.build_request(event, identity, profile)
    request.profile = profile
    await ctx.dispatcher.push_roast(request)
    text = ctx.messages[-1]["parts"][0]["text"]
    assert "first seen" not in text.lower()
    assert "Never invent first visits or meeting dates" in text


def test_first_seen_date_is_omitted_for_unrelated_questions():
    from datetime import date, timedelta

    from plugin.plugins.neko_live.adapters.viewer_evidence import current_evidence_rules, earliest_record_date

    today = date.today()
    prior = today - timedelta(days=60)
    profile = ViewerProfile(uid="B", nickname="Baka", first_seen_at=f"{prior.isoformat()}T12:00:00+00:00")
    assert not any("first seen" in rule.lower() for rule in current_evidence_rules("你好", has_avatar_image=False, profile=profile))
    assert earliest_record_date(f"{today.isoformat()}T12:00:00+00:00", today=today) == ""
    assert earliest_record_date("not-a-date") == ""
    assert earliest_record_date(f"{prior.isoformat()}T12:00:00+00:00", today=today) == prior.isoformat()
