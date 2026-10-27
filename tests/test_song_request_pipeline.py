"""Song request (点歌) intercept in the live pipeline flow."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.neko_live.adapters.neko_dispatcher import NekoDispatcher
from plugin.plugins.neko_live.core.contracts import (
    InteractionRequest,
    LiveConfig,
    ViewerEvent,
    ViewerIdentity,
    ViewerProfile,
)
from plugin.plugins.neko_live.core.permission_gate import PermissionGate
from plugin.plugins.neko_live.core.pipeline import LivePipeline
from plugin.plugins.neko_live.core.safety_guard import SafetyDecision
from plugin.plugins.neko_live.modules._base import ReservedModule


@dataclass(frozen=True)
class FakeOutcome:
    """Local mirror of SongRequestOutcome; the real module is not imported."""

    status: str
    query: str
    title: str = ""
    artist: str = ""
    playlist_total: int = 0
    cooldown_remaining: int = 0
    request_id: str = ""


class FakeSongRequest:
    def __init__(self, outcome: FakeOutcome, trigger: str = "点歌") -> None:
        self.outcome = outcome
        self.trigger = trigger
        self.handled: list[str] = []

    def match(self, text, *, event_type="danmaku", config=None):
        text = str(text or "").strip()
        if not text.startswith(self.trigger):
            return None
        return text[len(self.trigger):].strip()

    async def handle(self, ctx, event, query):
        self.handled.append(query)
        return self.outcome


def _request(event, identity, profile, label):
    return InteractionRequest(
        event=event,
        identity=identity,
        profile=profile,
        prompt_text=f"[NEKO Live {label}]\nRules:\n- Output only NEKO's line.",
        live_mode=event.live_mode,
        strength="normal",
    )


def _ctx(*, song_request=None, already_roasted=False, dispatcher=None):
    identity = ViewerIdentity(uid="42", nickname="test viewer")
    profile = ViewerProfile(uid="42", nickname="test viewer")
    config = LiveConfig(live_enabled=True, roast_once_per_uid=True, dry_run=False)
    pushed: list[InteractionRequest] = []

    async def push_roast(request):
        pushed.append(request)
        return "queued_to_neko(test)"

    viewer_profile = SimpleNamespace(
        upsert=AsyncMock(return_value=profile),
        has_roasted=AsyncMock(return_value=already_roasted),
        mark_roasted=AsyncMock(),
    )
    ctx = SimpleNamespace(
        config=config,
        permission_gate=PermissionGate(config),
        audit=SimpleNamespace(record=lambda *args, **kwargs: None),
        safety_guard=SimpleNamespace(
            before_event=lambda _: SafetyDecision(True),
            before_output=lambda _: SafetyDecision(True),
            after_event=lambda: None,
            record_failure=lambda *args: None,
        ),
        bili_identity=SimpleNamespace(resolve=AsyncMock(return_value=identity)),
        viewer_profile=viewer_profile,
        avatar_roast=SimpleNamespace(
            build_request=lambda e, i, p: _request(e, i, p, "first-appearance roast")
        ),
        dispatcher=dispatcher or SimpleNamespace(push_roast=push_roast),
        results=[],
    )
    if song_request is not None:
        ctx.song_request = song_request
    ctx.record_result = ctx.results.append
    return ctx, pushed


def _event(text):
    return ViewerEvent(
        uid="42",
        nickname="test viewer",
        danmaku_text=text,
        source="live_danmaku",
        target_lanlan="CatA",
        raw={"event_type": "danmaku"},
    )


@pytest.mark.asyncio
async def test_queued_song_request_dispatches_song_request_route_with_facts():
    module = FakeSongRequest(
        FakeOutcome(status="queued", query="晴天", title="晴天", artist="周杰伦", playlist_total=3)
    )
    ctx, pushed = _ctx(song_request=module)

    result = await LivePipeline(ctx).handle_event(_event("点歌 晴天"))

    assert result.status == "pushed"
    assert module.handled == ["晴天"]
    assert len(pushed) == 1
    request = pushed[0]
    assert request.metadata["response_module_hint"] == "song_request"
    assert request.metadata["song_request_status"] == "queued"
    assert "[NEKO Live song request reply]" in request.prompt_text
    assert "song_request_status: queued" in request.prompt_text
    assert "queued_song_title: 晴天" in request.prompt_text
    assert "queued_song_artist: 周杰伦" in request.prompt_text
    assert "do not claim it is playing now" in request.prompt_text
    assert any(step.id == "song_request" for step in result.steps)
    # 点歌回复不消耗首次锐评。
    ctx.viewer_profile.mark_roasted.assert_not_awaited()


@pytest.mark.asyncio
async def test_song_request_cooldown_skips_without_dispatch():
    module = FakeSongRequest(FakeOutcome(status="cooldown", query="晴天", cooldown_remaining=12))
    ctx, pushed = _ctx(song_request=module)

    result = await LivePipeline(ctx).handle_event(_event("点歌 晴天"))

    assert result.status == "skipped"
    assert result.reason == "song_request.cooldown"
    assert pushed == []
    assert ctx.results == [result]
    ctx.viewer_profile.mark_roasted.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_matching_danmaku_keeps_normal_routing():
    module = FakeSongRequest(FakeOutcome(status="queued", query="x"))
    ctx, pushed = _ctx(song_request=module)

    result = await LivePipeline(ctx).handle_event(_event("hello"))

    assert result.status == "pushed"
    assert module.handled == []
    assert len(pushed) == 1
    assert pushed[0].metadata["response_module_hint"] != "song_request"
    assert "first-appearance roast" in pushed[0].prompt_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "song_request",
    [None, ReservedModule("song_request", "Song request"), SimpleNamespace(match=None)],
)
async def test_missing_or_placeholder_module_keeps_normal_routing(song_request):
    ctx, pushed = _ctx(song_request=song_request)

    result = await LivePipeline(ctx).handle_event(_event("点歌 晴天"))

    assert result.status == "pushed"
    assert len(pushed) == 1
    assert pushed[0].metadata["response_module_hint"] != "song_request"


@pytest.mark.asyncio
async def test_already_roasted_viewer_still_gets_song_reply():
    # 对照组：非点歌弹幕在同一配置下会被 already_roasted 拦下。
    ctx, pushed = _ctx(song_request=FakeSongRequest(FakeOutcome("queued", "x")), already_roasted=True)
    baseline = await LivePipeline(ctx).handle_event(_event("hello"))
    assert baseline.status == "skipped"
    assert pushed == []

    module = FakeSongRequest(FakeOutcome(status="not_found", query="不存在的歌"))
    ctx, pushed = _ctx(song_request=module, already_roasted=True)

    result = await LivePipeline(ctx).handle_event(_event("点歌 不存在的歌"))

    assert result.status == "pushed"
    assert len(pushed) == 1
    assert pushed[0].metadata["song_request_status"] == "not_found"
    assert "local song library" in pushed[0].prompt_text
    ctx.viewer_profile.mark_roasted.assert_not_awaited()


@pytest.mark.asyncio
async def test_song_request_reply_goes_through_real_dispatcher_with_song_request_hint():
    messages = []

    def submit(**kwargs):
        messages.append(kwargs)
        return {"submitted": True}

    module = FakeSongRequest(FakeOutcome(status="empty_query", query=""))
    ctx, _ = _ctx(
        song_request=module,
        dispatcher=NekoDispatcher(SimpleNamespace(push_message=submit)),
    )

    result = await LivePipeline(ctx).handle_event(_event("点歌"))

    assert result.status == "pushed"
    assert len(messages) == 1
    assert "song_request_status: empty_query" in repr(messages[0])
    assert "song_request" in repr(messages[0].get("metadata"))
