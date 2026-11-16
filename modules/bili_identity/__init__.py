"""Resolve the v0.1 Bilibili identity fields."""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import socket
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ...core.avatar_image import MAX_AVATAR_BYTES as _MAX_AVATAR_BYTES
from ...core.avatar_image import open_avatar_image
from ...core.contracts import ViewerEvent, ViewerIdentity
from .._base import BaseModule
from ..danmaku_response.avatar_request import declines_own_avatar, requests_own_avatar

_MAX_AVATAR_REDIRECTS = 2
_BILI_AVATAR_HOST_SUFFIX = "hdslb.com"
_PROFILE_HINT_CACHE_LIMIT = 128
_PROFILE_HINT_CACHE_SECONDS = 15 * 60.0


def _external_http_client():
    # Keep the host dependency lazy so importing the plugin does not initialize
    # the shared TLS/proxy client until a Bilibili avatar is actually needed.
    from utils.external_http_client import get_external_http_client

    return get_external_http_client()


class BiliIdentityModule(BaseModule):
    id = "bili_identity"
    title = "B站身份解析"

    def __init__(self) -> None:
        super().__init__()
        self._profile_hints: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
        self._profile_hint_hits = 0
        self._profile_hint_misses = 0

    async def teardown(self) -> None:
        self._profile_hints.clear()
        self._profile_hint_hits = 0
        self._profile_hint_misses = 0
        await super().teardown()

    async def resolve(
        self,
        event: ViewerEvent,
        *,
        fetch_avatar_image: bool = True,
    ) -> ViewerIdentity:
        uid = str(event.uid or "").strip()
        nickname = str(event.nickname or "").strip()
        avatar_url = str(event.avatar_url or "").strip()
        display_name = nickname
        email = ""
        pendant = ""
        errors: list[str] = []
        should_fetch_avatar = self._should_fetch_avatar_image(
            event,
            requested=fetch_avatar_image,
        )

        if uid and uid.isdigit() and (not nickname or (should_fetch_avatar and not avatar_url)):
            try:
                profile = await self._profile_by_uid(uid)
                display_name = str(profile.get("name") or nickname or uid).strip()
                email = str(profile.get("email") or profile.get("mail") or "").strip()
                pendant = str(profile.get("pendant") or "").strip()
                nickname = nickname or display_name or uid
                avatar_url = avatar_url or str(profile.get("face") or "").strip()
                if self.ctx:
                    self.ctx.audit.record("bili_identity_fetched", "bili identity fetched", detail={"uid": uid})
            except Exception as exc:
                errors.append(f"profile_fetch_failed: {type(exc).__name__}")
                if self.ctx:
                    self.ctx.audit.record(
                        "bili_identity_fetch_failed",
                        f"profile fetch failed: {type(exc).__name__}",
                        level="warning",
                        detail={"uid": uid},
                    )

        nickname = nickname or uid
        display_name = display_name or nickname
        identity = ViewerIdentity(
            uid=uid,
            nickname=nickname,
            name=display_name,
            email=email,
            avatar_url=avatar_url,
            source_url=f"https://space.bilibili.com/{uid}" if uid else "",
            fetched=not errors,
            error="; ".join(errors),
            is_default_avatar=bool(avatar_url) and "noface" in avatar_url.lower(),
            pendant=pendant,
        )
        if not should_fetch_avatar:
            return identity
        if not avatar_url or identity.is_default_avatar:
            return identity
        avatar_cache = self.ctx.avatar_cache if self.ctx else None
        cached = avatar_cache.get(avatar_url) if avatar_cache is not None else None
        if cached:
            # 缓存只收解码验证过的头像，命中时不再重复解码。
            data, mime = cached
            identity.avatar_bytes = data
            identity.avatar_mime = mime
            identity.is_animated_avatar = bool(avatar_cache.is_animated(avatar_url))
            return identity
        timeout = self.ctx.config.avatar_fetch_timeout_seconds if self.ctx else 8
        try:
            if self._is_bili_avatar_url(avatar_url):
                data, mime = await self._fetch_bili_avatar(avatar_url, timeout)
            else:
                data, mime = await self._fetch_avatar(avatar_url, timeout)
            if data:
                usable, animated = await asyncio.to_thread(self._inspect_avatar, data)
                if not usable:
                    raise ValueError("avatar_decode_failed")
                identity.avatar_bytes = data
                identity.avatar_mime = mime
                identity.is_animated_avatar = animated
                ctx = self.ctx
                if ctx is not None:
                    ctx.avatar_cache.put(avatar_url, data, mime, animated=animated)
        except Exception as exc:
            identity.fetched = False
            avatar_error = f"avatar_fetch_failed: {type(exc).__name__}"
            identity.error = "; ".join([item for item in [identity.error, avatar_error] if item])
            ctx = self.ctx
            if ctx is not None:
                ctx.audit.record("avatar_fetch_failed", identity.error, level="warning", detail={"uid": uid})
        return identity

    def _should_fetch_avatar_image(
        self,
        event: ViewerEvent,
        *,
        requested: bool,
    ) -> bool:
        if not requested:
            return False
        if self.ctx is None:
            return True
        if not bool(getattr(self.ctx.config, "avatar_analysis_enabled", True)):
            return False
        if declines_own_avatar(event.danmaku_text or ""):
            return False
        live_avatar_roast_enabled = bool(
            getattr(self.ctx.config, "avatar_roast_enabled", True)
        )
        return live_avatar_roast_enabled or requests_own_avatar(event.danmaku_text or "") or event.source not in {
            "live_danmaku",
            "manual_live_simulation",
        }

    async def _profile_by_uid(self, uid: str) -> dict[str, str]:
        now = time.monotonic()
        self._prune_profile_hints(now)
        cached = self._profile_hints.get(uid)
        if cached is not None and cached[0] > now:
            self._profile_hint_hits += 1
            return dict(cached[1])
        self._profile_hint_misses += 1
        profile = await self._fetch_profile_by_uid(uid)
        # Only retain the public fields required by the live response path.
        # Email and other provider payload fields never enter this cache.
        hint = {
            "name": str(profile.get("name") or "").strip()[:80],
            "face": str(profile.get("face") or "").strip()[:512],
            "pendant": str(profile.get("pendant") or "").strip()[:80],
        }
        self._profile_hints[uid] = (now + _PROFILE_HINT_CACHE_SECONDS, hint)
        self._profile_hints.move_to_end(uid)
        while len(self._profile_hints) > _PROFILE_HINT_CACHE_LIMIT:
            self._profile_hints.popitem(last=False)
        return dict(hint)

    def _prune_profile_hints(self, now: float) -> None:
        while self._profile_hints:
            first_uid = next(iter(self._profile_hints))
            if self._profile_hints[first_uid][0] > now:
                break
            self._profile_hints.popitem(last=False)

    async def _fetch_profile_by_uid(self, uid: str) -> dict[str, Any]:
        from bilibili_api import user

        # 直播链路要求登录；开发者查询若没有凭据则由 provider 自身安全降级。
        credential = getattr(self.ctx, "bili_credential", None) if self.ctx else None
        target = user.User(uid=int(uid), credential=credential)
        info = await target.get_user_info()
        pendant = info.get("pendant") if isinstance(info.get("pendant"), dict) else {}
        return {
            "uid": str(info.get("mid") or uid),
            "name": str(info.get("name") or ""),
            "email": str(info.get("email") or info.get("mail") or ""),
            "face": str(info.get("face") or ""),
            # 挂件/装扮（出框头像的来源）；无装扮时 name 为空字符串。
            "pendant": str(pendant.get("name") or "").strip(),
        }

    @staticmethod
    def _inspect_avatar(data: bytes | None) -> tuple[bool, bool]:
        """Return (usable_for_vision, animated). Decode failures disable vision."""
        if not data:
            return False, False
        try:
            with open_avatar_image(data) as im:
                return True, bool(getattr(im, "is_animated", False))
        except Exception:
            return False, False

    @staticmethod
    async def _fetch_avatar(url: str, timeout: float) -> tuple[bytes, str]:
        if url == "neko-live://fixtures/demo-avatar":
            return BiliIdentityModule._load_demo_avatar()
        import httpx

        async with asyncio.timeout(timeout):
            parsed, resolved_ip, port = await asyncio.to_thread(BiliIdentityModule._resolve_avatar_endpoint, url)
            ip_host = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip
            pinned_url = urllib.parse.urlunparse(parsed._replace(netloc=f"{ip_host}:{port}", fragment=""))
            headers = {
                "Host": parsed.netloc,
                "Referer": "https://www.bilibili.com",
                "User-Agent": "Mozilla/5.0 NEKO-Roast/0.1",
                "Accept-Encoding": "identity",
            }
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                async with client.stream(
                    "GET", pinned_url, headers=headers, follow_redirects=False,
                    extensions={"sni_hostname": parsed.hostname},
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise ValueError("avatar_redirect_not_allowed")
                    return await BiliIdentityModule._read_avatar_response(response, url)

    @staticmethod
    async def _read_avatar_response(response: Any, url: str) -> tuple[bytes, str]:
        if response.status_code >= 400:
            raise ValueError("avatar_fetch_failed_status")
        encoding = str(response.headers.get("content-encoding") or "identity").strip().lower()
        if encoding != "identity":
            raise ValueError("avatar_content_encoding_not_allowed")
        content_length = str(response.headers.get("content-length") or "").strip()
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > _MAX_AVATAR_BYTES:
                raise ValueError("avatar_too_large")
        data = bytearray()
        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
            if len(data) + len(chunk) > _MAX_AVATAR_BYTES:
                raise ValueError("avatar_too_large")
            data.extend(chunk)
        content_type = str(response.headers.get("content-type") or "")
        mime = content_type.split(";", 1)[0].strip()
        return bytes(data), mime or mimetypes.guess_type(url)[0] or "image/png"

    @staticmethod
    def _is_bili_avatar_url(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
            port = parsed.port
        except (TypeError, ValueError):
            return False
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return False
        hostname = (parsed.hostname or "").strip(".").lower()
        if not hostname or port not in {None, 80, 443}:
            return False
        return hostname == _BILI_AVATAR_HOST_SUFFIX or hostname.endswith(
            f".{_BILI_AVATAR_HOST_SUFFIX}"
        )

    @staticmethod
    async def _fetch_bili_avatar(url: str, timeout: float) -> tuple[bytes, str]:
        async with asyncio.timeout(timeout):
            return await BiliIdentityModule._fetch_bili_avatar_stream(url, timeout)

    @staticmethod
    async def _fetch_bili_avatar_stream(url: str, timeout: float) -> tuple[bytes, str]:
        """Fetch allowlisted Bilibili CDN images through the host proxy client.

        Clash-style Fake-IP DNS intentionally resolves CDN hosts into the
        reserved 198.18.0.0/15 range. The generic downloader must reject those
        addresses for SSRF safety, while this path is safe because every request
        and redirect remains constrained to Bilibili's avatar CDN suffix.
        """

        current_url = url
        client = _external_http_client()
        headers = {
            "Referer": "https://www.bilibili.com",
            "User-Agent": "Mozilla/5.0 NEKO-Roast/0.1",
            "Accept-Encoding": "identity",
        }
        for _ in range(_MAX_AVATAR_REDIRECTS + 1):
            if not BiliIdentityModule._is_bili_avatar_url(current_url):
                raise ValueError("avatar_url_host_not_allowed")
            async with client.stream(
                "GET",
                current_url,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                final_url = str(getattr(response, "url", current_url) or current_url)
                if not BiliIdentityModule._is_bili_avatar_url(final_url):
                    raise ValueError("avatar_redirect_not_allowed")
                status = int(getattr(response, "status_code", 0) or 0)
                if 300 <= status < 400:
                    location = str(response.headers.get("location") or "").strip()
                    if not location:
                        raise ValueError("avatar_redirect_missing_location")
                    next_url = urllib.parse.urljoin(final_url, location)
                    if not BiliIdentityModule._is_bili_avatar_url(next_url):
                        raise ValueError("avatar_redirect_not_allowed")
                    current_url = next_url
                    continue
                return await BiliIdentityModule._read_avatar_response(response, final_url)
        raise ValueError("avatar_too_many_redirects")

    @staticmethod
    def _resolve_avatar_endpoint(url: str) -> tuple[urllib.parse.ParseResult, str, int]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("avatar_url_scheme_not_allowed")
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("avatar_url_host_required")
        lowered = hostname.lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise ValueError("avatar_url_host_not_allowed")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = [item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)]
        except OSError as exc:
            raise ValueError("avatar_url_host_unresolved") from exc
        if not addresses:
            raise ValueError("avatar_url_host_unresolved")
        for address in set(addresses):
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise ValueError("avatar_url_host_not_allowed")
        return parsed, addresses[0], port

    @staticmethod
    def _validate_avatar_url(url: str) -> None:
        BiliIdentityModule._resolve_avatar_endpoint(url)

    @staticmethod
    def _load_demo_avatar() -> tuple[bytes, str]:
        plugin_root = Path(__file__).resolve().parents[2]
        png_path = plugin_root / "fixtures" / "demo_avatar.png"
        if png_path.is_file():
            return png_path.read_bytes(), "image/png"
        svg_path = plugin_root / "fixtures" / "demo_avatar.svg"
        return svg_path.read_bytes(), "image/svg+xml"

    def status(self) -> dict[str, Any]:
        self._prune_profile_hints(time.monotonic())
        avatar_cache = getattr(self.ctx, "avatar_cache", None) if self.ctx else None
        avatar_status = getattr(avatar_cache, "status", None)
        return {
            "enabled": self.enabled,
            "avatar_cache": avatar_status() if callable(avatar_status) else {},
            "profile_hint_cache": {
                "items": len(self._profile_hints),
                "max_items": _PROFILE_HINT_CACHE_LIMIT,
                "ttl_seconds": _PROFILE_HINT_CACHE_SECONDS,
                "hits": self._profile_hint_hits,
                "misses": self._profile_hint_misses,
            },
        }
