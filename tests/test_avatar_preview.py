from __future__ import annotations

import asyncio
import base64
import io
import json
from threading import get_ident
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image, PngImagePlugin
from plugin.plugins.neko_live.core import avatar_image, runtime_developer_tools
from plugin.plugins.neko_live.core.contracts import ViewerIdentity


def avatar_bytes(image_format="PNG"):
    buffer = io.BytesIO()
    image = Image.new("RGB", (400, 200), "blue")
    if image_format == "JPEG":
        exif = Image.Exif()
        exif[270] = "private image metadata"
        image.save(buffer, format=image_format, exif=exif)
    elif image_format == "PNG":
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("comment", "private image metadata")
        image.save(buffer, format=image_format, pnginfo=metadata)
    else:
        image.save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "GIF", "WEBP"])
async def test_explicit_lookup_returns_only_a_bounded_reencoded_thumbnail(runtime, monkeypatch, image_format):
    runtime.config.developer_tools_enabled = True
    source = avatar_bytes(image_format)
    identity = ViewerIdentity(uid="42", nickname="viewer", avatar_url="https://example.invalid/avatar", avatar_bytes=source)
    resolve = AsyncMock(return_value=identity)
    monkeypatch.setattr(runtime.bili_identity, "resolve", resolve)
    normalize = runtime_developer_tools.normalize_avatar_jpeg
    loop_thread = get_ident()

    def on_worker(*args, **kwargs):
        assert get_ident() != loop_thread
        return normalize(*args, **kwargs)

    monkeypatch.setattr(runtime_developer_tools, "normalize_avatar_jpeg", on_worker)
    result = await runtime_developer_tools.lookup_bili_user(runtime, uid="42", include_avatar_preview=True)
    data_url = result.pop("avatar_preview_data_url")
    assert data_url.startswith("data:image/jpeg;base64,")
    thumbnail = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    assert len(thumbnail) <= avatar_image.MAX_AVATAR_PREVIEW_BYTES
    assert thumbnail != source
    with Image.open(io.BytesIO(thumbnail)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (144, 72)
        assert decoded.mode == "RGB"
        assert not decoded.getexif()
        assert "comment" not in decoded.info
    resolve.assert_awaited_once()
    assert "avatar_bytes" not in result["identity"]
    assert result["identity"]["avatar_preview_data_url"] == ""
    assert "base64" not in json.dumps(result)
    assert "base64" not in json.dumps(runtime.audit.recent())
    assert not runtime.recent_sandbox_results


@pytest.mark.asyncio
@pytest.mark.parametrize("include_preview", [False, "true", 1, None])
async def test_lookup_does_not_generate_or_return_images_without_explicit_boolean(runtime, monkeypatch, include_preview):
    runtime.config.developer_tools_enabled = True
    monkeypatch.setattr(runtime.bili_identity, "resolve", AsyncMock(return_value=ViewerIdentity(
        uid="42", nickname="viewer", avatar_bytes=avatar_bytes(),
    )))
    normalize = Mock(side_effect=AssertionError("preview was not explicitly requested"))
    monkeypatch.setattr(runtime_developer_tools, "normalize_avatar_jpeg", normalize)
    result = await runtime_developer_tools.lookup_bili_user(runtime, uid="42", include_avatar_preview=include_preview)
    assert "avatar_preview_data_url" not in result
    normalize.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["invalid", "missing", "default", "pixel_limit", "byte_limit", "analysis_disabled"])
async def test_unusable_preview_is_empty_without_external_url_fallback(runtime, monkeypatch, case):
    runtime.config.developer_tools_enabled = True
    identity = ViewerIdentity(uid="42", nickname="viewer", avatar_url="https://example.invalid/avatar", avatar_bytes=avatar_bytes())
    if case == "invalid":
        identity.avatar_bytes = b"<svg onload='must-not-run'/>"
    elif case == "missing":
        identity.avatar_bytes = None
    elif case == "default":
        identity.is_default_avatar = True
    elif case == "pixel_limit":
        monkeypatch.setattr(avatar_image, "MAX_AVATAR_PIXELS", 4)
    elif case == "byte_limit":
        monkeypatch.setattr(avatar_image, "MAX_AVATAR_BYTES", 4)
    else:
        runtime.config.avatar_analysis_enabled = False
    monkeypatch.setattr(runtime.bili_identity, "resolve", AsyncMock(return_value=identity))
    result = await runtime_developer_tools.lookup_bili_user(runtime, uid="42", include_avatar_preview=True)
    assert result["avatar_preview_data_url"] == ""
    assert result["identity"]["avatar_preview_url"] == ""


@pytest.mark.asyncio
async def test_developer_disable_during_lookup_rejects_the_inflight_result(runtime, monkeypatch):
    runtime.config.developer_tools_enabled = True
    started = asyncio.Event()
    resume = asyncio.Event()

    async def resolve(_event):
        started.set()
        await resume.wait()
        return ViewerIdentity(uid="42", nickname="viewer", avatar_bytes=avatar_bytes())

    monkeypatch.setattr(runtime.bili_identity, "resolve", resolve)
    task = asyncio.create_task(runtime_developer_tools.lookup_bili_user(runtime, uid="42", include_avatar_preview=True))
    await started.wait()
    runtime.config.developer_tools_enabled = False
    resume.set()
    with pytest.raises(PermissionError):
        await task
    assert not any(item["op"] == "developer_lookup" for item in runtime.audit.recent())


def test_jpeg_normalization_honors_output_budget_and_transparency():
    raw = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(raw, format="PNG")
    assert avatar_image.normalize_avatar_jpeg(raw.getvalue(), budget_bytes=1) == b""
    jpeg = avatar_image.normalize_avatar_jpeg(raw.getvalue())
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)
