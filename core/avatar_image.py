"""Shared limits and JPEG normalization for viewer avatars."""

from __future__ import annotations

import io
from contextlib import contextmanager

MAX_AVATAR_BYTES = 1024 * 1024
MAX_AVATAR_PIXELS = 2048 * 2048
MAX_AVATAR_PREVIEW_BYTES = 12 * 1024


@contextmanager
def open_avatar_image(data: bytes):
    """Check encoded size and pixel count before decoding the first frame."""
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError("avatar_too_large")
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > MAX_AVATAR_PIXELS:
            raise ValueError("avatar_pixel_limit_exceeded")
        image.load()
        yield image


def normalize_avatar_jpeg(
    data: bytes,
    *,
    edges: tuple[int, ...] = (384, 256, 192),
    budget_bytes: int = 64 * 1024,
) -> bytes:
    """Decode one bounded frame, strip metadata and fit a JPEG output budget."""
    if not data:
        return b""
    try:
        from PIL import Image

        with open_avatar_image(data) as source:
            image = Image.new("RGB", source.size, (255, 255, 255))
            if source.mode in {"RGBA", "LA", "P"}:
                rgba = source.convert("RGBA")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image.paste(source.convert("RGB"))
            for edge in edges:
                frame = image.copy()
                frame.thumbnail((edge, edge))
                for quality in (80, 68, 56):
                    buffer = io.BytesIO()
                    frame.save(buffer, format="JPEG", quality=quality, optimize=True)
                    candidate = buffer.getvalue()
                    if len(candidate) <= budget_bytes:
                        return candidate
    except Exception:
        return b""
    return b""
