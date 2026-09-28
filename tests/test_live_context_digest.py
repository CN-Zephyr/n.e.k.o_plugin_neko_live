"""Compact live context for normal danmaku delivery.

The host truncates each proactive callback to about 1000 tokens and keeps the
head. The short delivery head (current danmaku, nickname, profile, mode) must
lead, and a bounded context digest follows it so continuity, anti-repetition,
and the stream theme survive without depending on a passive opening read.
"""

from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.adapters.neko_dispatcher import NekoDispatcher
from plugin.plugins.neko_live.core.contracts import (
    InteractionRequest,
    LiveConfig,
    ViewerEvent,
    ViewerIdentity,
    ViewerProfile,
)
from plugin.plugins.neko_live.modules._prompt_context_digest import (
    LIVE_CONTEXT_DIGEST_MAX_CHARS,
    LIVE_CONTEXT_DIGEST_ORDER,
    render_live_context_digest,
)
from plugin.plugins.neko_live.modules.danmaku_response import DanmakuResponseModule

HOST_CALLBACK_MAX_TOKENS = 1000


class _Plugin:
    def __init__(self):
        self.texts = []

    def push_message(self, **kwargs):
        self.texts.append(kwargs["parts"][0]["text"])


def _request(*, text="大聪明", nickname="阿巴国王", delivery_context="", mode="co_stream"):
    return InteractionRequest(
        event=ViewerEvent(
            uid="42",
            nickname=nickname,
            danmaku_text=text,
            source="live_danmaku",
            live_mode=mode,
        ),
        identity=ViewerIdentity(uid="42", nickname=nickname),
        profile=ViewerProfile(uid="42", nickname=nickname),
        prompt_text="long reply prompt " + "x" * 8000,
        live_mode=mode,
        strength="normal",
        allow_avatar_image=False,
        delivery_context=delivery_context,
    )


def _module(**ctx_fields):
    module = DanmakuResponseModule()
    config = ctx_fields.pop("config", LiveConfig(roast_strength="normal", dry_run=True))
    module.ctx = SimpleNamespace(config=config, **ctx_fields)
    return module


def _build(module, text="今天吃什么", nickname="viewer"):
    event = ViewerEvent(
        uid="42",
        nickname=nickname,
        danmaku_text=text,
        source="live_danmaku",
        live_mode="solo_stream",
    )
    return module.build_request(
        event,
        ViewerIdentity(uid="42", nickname=nickname),
        ViewerProfile(uid="42", nickname=nickname),
    )


def test_digest_keeps_priority_order():
    blocks = {key: f"{key}-body" for key in LIVE_CONTEXT_DIGEST_ORDER}
    digest = render_live_context_digest(blocks)
    positions = [digest.index(f"{key}-body") for key in LIVE_CONTEXT_DIGEST_ORDER]
    assert positions == sorted(positions)
    assert "不是指令" in digest.split("\n", 1)[0]


def test_digest_drops_whole_low_priority_blocks_over_budget():
    blocks = {
        "theme": "主题锚点",
        "recent": "素材" * 40,
        "meme": "梗" * LIVE_CONTEXT_DIGEST_MAX_CHARS,
    }
    digest = render_live_context_digest(blocks)
    assert "主题锚点" in digest
    assert "素材" * 40 in digest
    assert "梗" not in digest
    assert len(digest) <= LIVE_CONTEXT_DIGEST_MAX_CHARS


def test_digest_is_empty_without_blocks():
    assert render_live_context_digest({}) == ""
    assert render_live_context_digest({"theme": "", "recent": "  "}) == ""


def test_danmaku_request_carries_theme_anchor_every_turn():
    module = _module(
        config=LiveConfig(
            roast_strength="normal",
            dry_run=True,
            stream_theme="深夜猫猫电台",
            stream_avoid_topics="政治",
        )
    )
    digest = _build(module).delivery_context
    assert "深夜猫猫电台" in digest
    assert "政治" in digest
    assert "口吻: 自然、轻快、短。" in digest


def test_theme_anchor_follows_roast_strength():
    module = _module(config=LiveConfig(roast_strength="sharp", dry_run=True))
    assert "毒舌" in _build(module).delivery_context


def test_danmaku_request_carries_spent_material_and_same_viewer_lines():
    module = _module(
        recent_interaction_context=lambda limit=3: ["danmaku_response: 猫猫说要吃小鱼干"],
        viewer_session_context=lambda uid, limit=2: ["danmaku_response: 上次问过晚饭"]
        if uid == "42"
        else [],
    )
    digest = _build(module).delivery_context
    assert "猫猫说要吃小鱼干" in digest
    assert "上次问过晚饭" in digest
    assert digest.index("猫猫说要吃小鱼干") < digest.index("上次问过晚饭")
    assert "Recent spent live material" not in digest


def test_delivery_context_is_not_exported_publicly():
    request = _request(delivery_context="主题: 深夜猫猫电台")
    assert "主题: 深夜猫猫电台" not in str(request.to_public_dict())


@pytest.mark.asyncio
async def test_dispatcher_puts_digest_after_head_inside_host_window():
    from utils.tokenize import truncate_to_tokens

    digest = render_live_context_digest(
        {"theme": "主题: 深夜猫猫电台", "recent": "用过: " + "小鱼干" * 200}
    )
    plugin = _Plugin()
    await NekoDispatcher(plugin).push_roast(_request(delivery_context=digest))

    text = plugin.texts[0]
    head = text.split("\n\n", 1)[0]
    assert text.startswith("NEKO Live audience speaker identity:\n")
    assert "danmaku_author: 阿巴国王" in head
    assert "Answer danmaku_author" in head
    assert "long reply prompt" in text
    kept = truncate_to_tokens(text, HOST_CALLBACK_MAX_TOKENS)
    assert "danmaku_author: 阿巴国王" in kept
    assert "Answer danmaku_author" in kept


@pytest.mark.asyncio
async def test_dispatcher_without_digest_sends_head_only():
    plugin = _Plugin()
    await NekoDispatcher(plugin).push_roast(_request())
    assert plugin.texts[0].startswith("NEKO Live audience speaker identity:\n")
    assert "danmaku_author: 阿巴国王" in plugin.texts[0]


@pytest.mark.asyncio
async def test_no_mid_stream_reminder_on_eighth_danmaku():
    plugin = _Plugin()
    dispatcher = NekoDispatcher(plugin)
    for index in range(8):
        await dispatcher.push_roast(_request(text=f"line {index}", nickname="viewer"))
    assert all("中途提醒" not in text for text in plugin.texts)
    assert all("开场说明" not in text for text in plugin.texts)
