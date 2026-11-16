from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse

import httpx
import pytest
from PIL import Image, PngImagePlugin
from plugin.plugins.neko_live.adapters.neko_dispatcher import NekoDispatcher, _normalize_avatar_for_neko_vision
from plugin.plugins.neko_live.core import avatar_image
from plugin.plugins.neko_live.core.contracts import InteractionRequest, ViewerEvent, ViewerIdentity, ViewerProfile
from plugin.plugins.neko_live.modules import bili_identity
from plugin.plugins.neko_live.modules.bili_identity import BiliIdentityModule


def test_pixel_budget_is_checked_before_both_avatar_decoders(monkeypatch):
    raw = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(raw, format="PNG")
    monkeypatch.setattr(avatar_image, "MAX_AVATAR_PIXELS", 63)
    load = Mock(side_effect=AssertionError("oversized image must not be decoded"))
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", load)

    assert BiliIdentityModule._inspect_avatar(raw.getvalue()) == (False, False)
    assert _normalize_avatar_for_neko_vision(raw.getvalue(), "image/png")[0] == b""
    load.assert_not_called()


def test_encoded_size_is_checked_before_opening_avatar(monkeypatch):
    monkeypatch.setattr(avatar_image, "MAX_AVATAR_BYTES", 16)
    open_image = Mock(side_effect=AssertionError("oversized image must not be opened"))
    monkeypatch.setattr(Image, "open", open_image)

    assert BiliIdentityModule._inspect_avatar(b"x" * 17) == (False, False)
    assert _normalize_avatar_for_neko_vision(b"x" * 17, "image/png")[0] == b""
    open_image.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("danmaku", ["hi", "describe my avatar"])
async def test_dispatcher_omits_invalid_avatar_and_marks_it_unavailable(danmaku):
    messages = []
    dispatcher = NekoDispatcher(SimpleNamespace(push_message=lambda **kwargs: messages.append(kwargs)))
    request = InteractionRequest(
        event=ViewerEvent(uid="42", nickname="viewer", source="live_danmaku", danmaku_text=danmaku),
        identity=ViewerIdentity(uid="42", nickname="viewer", avatar_bytes=b"invalid image"),
        profile=ViewerProfile(uid="42", nickname="viewer"),
        prompt_text="Reply to the current viewer.",
        live_mode="solo_stream",
        strength="normal",
        allow_avatar_image=True,
    )

    result = await dispatcher.push_roast(request)

    assert result.startswith("queued_to_neko(")
    assert all(part["type"] == "text" for part in messages[0]["parts"])
    text = messages[0]["parts"][0]["text"]
    assert "当前头像不可用或超出图片预算，本次请求没有附加头像" in text
    if danmaku != "hi":
        assert "Current viewer avatar: unavailable in this request" in text


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_small_avatars_decode_at_exact_pixel_budget(monkeypatch, image_format):
    monkeypatch.setattr(avatar_image, "MAX_AVATAR_PIXELS", 64)
    raw = io.BytesIO()
    image = Image.new("RGB", (8, 8), "blue")
    options = {"save_all": True, "append_images": [Image.new("RGB", (8, 8), "red")]} if image_format == "GIF" else {}
    image.save(raw, format=image_format, **options)

    assert BiliIdentityModule._inspect_avatar(raw.getvalue()) == (True, image_format == "GIF")
    normalized, mime = _normalize_avatar_for_neko_vision(raw.getvalue(), "")
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(normalized)) as decoded:
        assert decoded.size == (8, 8)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def avatar_transport(monkeypatch):
    client_type = httpx.AsyncClient

    def install(provider, handler):
        transport = httpx.MockTransport(handler)
        if provider == "bilibili":
            client = client_type(transport=transport)
            monkeypatch.setattr(bili_identity, "_external_http_client", lambda: client)
            return client, BiliIdentityModule._fetch_bili_avatar, "https://i0.hdslb.com/avatar.png"
        client = client_type(transport=transport)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
        monkeypatch.setattr(
            BiliIdentityModule, "_resolve_avatar_endpoint",
            staticmethod(lambda url: (urlparse(url), "8.8.8.8", 443)),
        )
        return client, BiliIdentityModule._fetch_avatar, "https://cdn.example.test/avatar.png"

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bilibili", "generic"])
async def test_avatar_total_timeout_cancels_slow_stream(avatar_transport, provider):
    stream = _Stream([b"x"] * 100, delay=0.01)
    client, fetch, url = avatar_transport(provider, lambda request: httpx.Response(200, stream=stream))
    try:
        with pytest.raises(TimeoutError):
            await fetch(url, timeout=0.05)
        assert stream.closed
        assert stream.reads < 100
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bilibili", "generic"])
@pytest.mark.parametrize("size", [16, 17])
async def test_avatar_stream_enforces_exact_byte_limit(monkeypatch, avatar_transport, provider, size):
    monkeypatch.setattr(bili_identity, "_MAX_AVATAR_BYTES", 16)
    stream = _Stream([b"x" * size])
    client, fetch, url = avatar_transport(provider, lambda request: httpx.Response(200, stream=stream))
    try:
        if size == 16:
            assert await fetch(url, timeout=1) == (b"x" * 16, "image/png")
        else:
            with pytest.raises(ValueError, match="avatar_too_large"):
                await fetch(url, timeout=1)
        assert stream.closed
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bilibili", "generic"])
@pytest.mark.parametrize("headers", [{"content-length": "1048577"}, {"content-encoding": "gzip"}])
async def test_avatar_rejects_oversized_or_encoded_body_before_reading(avatar_transport, provider, headers):
    stream = _Stream([b"unused body"])
    client, fetch, url = avatar_transport(
        provider, lambda request: httpx.Response(200, headers=headers, stream=stream),
    )
    try:
        with pytest.raises(ValueError, match="avatar_"):
            await fetch(url, timeout=1)
        assert stream.reads == 0
        assert stream.closed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_bilibili_redirects_share_one_download_deadline(avatar_transport):
    requests = []

    async def redirect(request):
        requests.append(request)
        await asyncio.sleep(0.03)
        return httpx.Response(302, headers={"location": "/next.png"})

    client, fetch, url = avatar_transport("bilibili", redirect)
    try:
        with pytest.raises(TimeoutError):
            await fetch(url, timeout=0.05)
        assert len(requests) <= 2
    finally:
        await client.aclose()
