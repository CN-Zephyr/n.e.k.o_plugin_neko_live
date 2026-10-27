"""Song request (点歌) module: match trigger danmaku and queue songs in custom_music_list."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from plugin.sdk.plugin import Err
from plugin.sdk.plugin.llm_tool import entry_id_for_tool

from ...core.contracts_public import public_text
from .._base import BaseModule
from .settings import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_TRIGGER_TEXT,
    MAX_COOLDOWN_SECONDS,
    normalize_match_text,
    normalize_permission,
    trigger_tuple,
)

MUSIC_PLUGIN_ID = "custom_music_list"
ADD_SONG_ENTRY = f"{MUSIC_PLUGIN_ID}:{entry_id_for_tool('add_song_to_playlist')}"
PLAYLIST_GET_ENTRY = f"{MUSIC_PLUGIN_ID}:playlist_get"
PLAYLIST_MOVE_ENTRY = f"{MUSIC_PLUGIN_ID}:playlist_move"
AUTOPLAY_STATUS_ENTRY = f"{MUSIC_PLUGIN_ID}:playlist_autoplay_status"

_CHAT_TYPES = {"", "danmaku", "danmu", "chat"}
_MUSIC_PLUGIN_DOWN_STATUSES = {"stopped", "crashed", "load_failed", "source_missing"}
_QUERY_SEPARATORS = " :：,，、"
_MAX_QUERY_LENGTH = 64
_MAX_RECENT = 50
_MAX_COOLDOWN_ENTRIES = 1024
_ADD_TIMEOUT = 15.0
_CALL_TIMEOUT = 5.0


@dataclass(frozen=True)
class SongRequestOutcome:
    """Result of one song request; consumed by the pipeline reply builder."""

    status: str  # queued / not_found / empty_query / denied / unavailable / error / cooldown
    query: str
    title: str = ""
    artist: str = ""
    playlist_total: int = 0
    cooldown_remaining: int = 0
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SongRequestModule(BaseModule):
    id = "song_request"
    title = "Song request"
    domain = "interaction"

    def __init__(self) -> None:
        super().__init__()
        self._clock = time.monotonic
        self._cooldowns: dict[str, float] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT)
        # request_id -> music plugin playlist item id, only for queued requests.
        self._queued_items: dict[str, str] = {}
        # 加歌后要读回歌单定位新条目；串行化避免两条点歌互相认错条目。
        self._queue_lock = asyncio.Lock()

    def match(self, text: str, *, event_type: str = "danmaku", config: Any = None) -> str | None:
        """Return the song query ("" when only the trigger was sent), or None when not a request."""
        config = config if config is not None else getattr(self.ctx, "config", None)
        if not self.enabled or getattr(config, "song_request_enabled", False) is not True:
            return None
        if str(event_type or "").strip().lower() not in _CHAT_TYPES:
            return None
        normalized = normalize_match_text(text or "")
        for trigger in trigger_tuple(getattr(config, "song_request_triggers", DEFAULT_TRIGGER_TEXT)):
            if normalized.startswith(trigger):
                return normalized[len(trigger):].strip(_QUERY_SEPARATORS)[:_MAX_QUERY_LENGTH]
        return None

    async def handle(self, ctx: Any, event: Any, query: str) -> SongRequestOutcome:
        """Apply cooldown, permission and queueing; never raises."""
        config = getattr(ctx, "config", None)
        uid = str(getattr(event, "uid", "") or "").strip()
        remaining = self._cooldown_remaining(uid, config)
        if remaining > 0:
            return SongRequestOutcome(status="cooldown", query=query, cooldown_remaining=remaining)
        # 每次得到回复的点歌（含没权限、没写歌名）都开始冷却，防止刷屏刷出连续回复。
        self._start_cooldown(uid, config)

        query = query.strip()
        permission = normalize_permission(getattr(config, "song_request_permission", "anyone"))
        if not self._permitted(event, permission, getattr(config, "live_platform", "bilibili")):
            outcome = SongRequestOutcome(status="denied", query=query)
        elif not query:
            outcome = SongRequestOutcome(status="empty_query", query=query)
        else:
            outcome = await self._queue_song(ctx, query)
        self._record(event, outcome)
        return outcome

    def status(self) -> dict[str, Any]:
        config = getattr(self.ctx, "config", None)
        return {
            "enabled": self.enabled,
            "active": bool(getattr(config, "song_request_enabled", False)),
            "recent_count": len(self._recent),
        }

    async def music_plugin_state(self, ctx: Any) -> str:
        """Install state of custom_music_list for the panel: ready / not_installed / not_running / unknown."""
        plugins = _plugins_accessor(ctx)
        lister = getattr(plugins, "list", None)
        if not callable(lister):
            return "unknown"
        try:
            listed = await lister(timeout=_CALL_TIMEOUT)
        except Exception:
            return "unknown"
        if isinstance(listed, Err) or not isinstance(getattr(listed, "value", None), list):
            return "unknown"
        descriptor = next((d for d in listed.value if isinstance(d, dict) and d.get("plugin_id") == MUSIC_PLUGIN_ID), None)
        if descriptor is None:
            return "not_installed"
        if descriptor.get("enabled") is False or descriptor.get("status") in _MUSIC_PLUGIN_DOWN_STATUSES:
            return "not_running"
        return "ready"

    def recent_requests(self) -> list[dict[str, Any]]:
        """Public rows for the panel, newest first."""
        rows = []
        for row in reversed(self._recent):
            item = dict(row)
            item["can_jump"] = row["request_id"] in self._queued_items
            rows.append(item)
        return rows

    async def jump(self, ctx: Any, request_id: str) -> dict[str, Any]:
        """Move a queued request to the slot right after the song now playing."""
        request_id = str(request_id or "").strip()
        item_id = self._queued_items.get(request_id)
        if not item_id:
            return {"ok": False, "reason": "request_not_queued"}
        plugins = _plugins_accessor(ctx)
        if plugins is None:
            return {"ok": False, "reason": "music_plugin_unavailable"}
        async with self._queue_lock:
            status = await _call_json(plugins, AUTOPLAY_STATUS_ENTRY, {})
            playlist = await _call_json(plugins, PLAYLIST_GET_ENTRY, {})
            if status is None or playlist is None:
                return {"ok": False, "reason": "music_plugin_unavailable"}
            songs = playlist.get("songs") if isinstance(playlist.get("songs"), list) else []
            index = next(
                (i for i, song in enumerate(songs) if isinstance(song, dict) and song.get("id") == item_id),
                None,
            )
            if index is None:
                self._queued_items.pop(request_id, None)
                return {"ok": False, "reason": "song_left_playlist"}
            current = status.get("current_index")
            if isinstance(current, bool) or not isinstance(current, int) or current < 0:
                # 只有自动下一首在跑时音乐插件才公开当前位置；拿不到就不猜。
                return {"ok": False, "reason": "current_song_unknown"}
            if index <= current:
                self._queued_items.pop(request_id, None)
                return {"ok": False, "reason": "already_played"}
            target = current + 1
            if index != target:
                moved = await _call_json(plugins, PLAYLIST_MOVE_ENTRY, {"item_id": item_id, "new_index": target})
                if moved is None:
                    return {"ok": False, "reason": "move_failed"}
        self._queued_items.pop(request_id, None)
        for row in self._recent:
            if row["request_id"] == request_id:
                row["jumped"] = True
                break
        return {"ok": True, "new_index": target}

    def config_schema(self) -> list[dict[str, Any]]:
        prefix = "panel.interaction.module.songRequest"
        return [
            {"name": "song_request_enabled", "type": "boolean", "label": f"{prefix}.title", "default": False},
            {"name": "song_request_triggers", "type": "text", "label": f"{prefix}.triggers", "default": DEFAULT_TRIGGER_TEXT},
            {
                "name": "song_request_permission",
                "type": "select",
                "label": f"{prefix}.permission",
                "default": "anyone",
                "options": [
                    {"value": value, "label": f"{prefix}.permission.{value}"}
                    for value in ("anyone", "fans", "guard")
                ],
            },
            {
                "name": "song_request_cooldown_seconds",
                "type": "string",
                "label": f"{prefix}.cooldown",
                "default": str(DEFAULT_COOLDOWN_SECONDS),
            },
        ]

    async def _queue_song(self, ctx: Any, query: str) -> SongRequestOutcome:
        plugins = _plugins_accessor(ctx)
        if plugins is None:
            return SongRequestOutcome(status="unavailable", query=query)
        try:
            required = await plugins.require_enabled(MUSIC_PLUGIN_ID, timeout=_CALL_TIMEOUT)
        except Exception:
            required = None
        if required is None or isinstance(required, Err):
            return SongRequestOutcome(status="unavailable", query=query)
        async with self._queue_lock:
            added = await _call_json(plugins, ADD_SONG_ENTRY, {"title": query}, timeout=_ADD_TIMEOUT)
            if added is None:
                return SongRequestOutcome(status="error", query=query)
            output = added.get("output") if isinstance(added.get("output"), dict) else added
            if added.get("is_error"):
                status = "not_found" if added.get("error") == "SONG_NOT_FOUND" else "error"
                return SongRequestOutcome(status=status, query=query)
            title = public_text(output.get("added_title"), max_len=120)
            artist = public_text(output.get("added_artist"), max_len=120)
            total = output.get("playlist_total")
            total = total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else 0
            request_id = f"sr_{uuid.uuid4().hex[:12]}"
            item_id = await self._locate_new_item(plugins, title, artist)
        if item_id:
            self._queued_items[request_id] = item_id
        return SongRequestOutcome(
            status="queued",
            query=query,
            title=title,
            artist=artist,
            playlist_total=total,
            request_id=request_id,
        )

    async def _locate_new_item(self, plugins: Any, title: str, artist: str) -> str:
        """Find the playlist item just appended; add_song_to_playlist does not return its id."""
        playlist = await _call_json(plugins, PLAYLIST_GET_ENTRY, {})
        songs = playlist.get("songs") if isinstance(playlist, dict) else None
        if not isinstance(songs, list):
            return ""
        tracked = set(self._queued_items.values())
        for song in reversed(songs):
            if not isinstance(song, dict):
                continue
            item_id = str(song.get("id") or "")
            if not item_id or item_id in tracked:
                continue
            if public_text(song.get("title"), max_len=120) == title and public_text(song.get("artist"), max_len=120) == artist:
                return item_id
        return ""

    def _cooldown_remaining(self, uid: str, config: Any) -> int:
        seconds = _cooldown_seconds(config)
        if not uid or seconds <= 0:
            return 0
        last = self._cooldowns.get(uid)
        if last is None:
            return 0
        remaining = seconds - (self._clock() - last)
        return max(0, int(remaining + 0.999))

    def _start_cooldown(self, uid: str, config: Any) -> None:
        seconds = _cooldown_seconds(config)
        if not uid or seconds <= 0:
            return
        now = self._clock()
        if len(self._cooldowns) >= _MAX_COOLDOWN_ENTRIES:
            self._cooldowns = {key: ts for key, ts in self._cooldowns.items() if now - ts < seconds}
        self._cooldowns[uid] = now

    @staticmethod
    def _permitted(event: Any, permission: str, platform: Any) -> bool:
        """fans = any Bilibili fan medal, guard = any captain tier; other platforms only allow "anyone"."""
        if permission == "anyone":
            return True
        if str(platform or "bilibili") != "bilibili":
            return False
        raw = getattr(event, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        guard = _positive_int(raw.get("guard_level"))
        if permission == "guard":
            return guard > 0
        return guard > 0 or _positive_int(raw.get("medal_level")) > 0

    def _record(self, event: Any, outcome: SongRequestOutcome) -> None:
        self._recent.append(
            {
                "request_id": outcome.request_id or f"sr_{uuid.uuid4().hex[:12]}",
                "uid": public_text(getattr(event, "uid", ""), max_len=128),
                "nickname": public_text(getattr(event, "nickname", ""), max_len=80),
                "query": public_text(outcome.query, max_len=_MAX_QUERY_LENGTH),
                "title": outcome.title,
                "artist": outcome.artist,
                "status": outcome.status,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "jumped": False,
            }
        )
        live_ids = {row["request_id"] for row in self._recent}
        for request_id in [key for key in self._queued_items if key not in live_ids]:
            self._queued_items.pop(request_id, None)


def _plugins_accessor(ctx: Any) -> Any:
    plugin = getattr(ctx, "plugin", None)
    return getattr(plugin, "plugins", None)


async def _call_json(plugins: Any, entry_ref: str, args: dict[str, Any], *, timeout: float = _CALL_TIMEOUT) -> dict[str, Any] | None:
    """Cross-plugin call returning the payload dict, or None on any failure."""
    try:
        result = await plugins.call_entry_json(entry_ref, args, timeout=timeout)
    except Exception:
        return None
    if isinstance(result, Err):
        return None
    value = getattr(result, "value", None)
    return value if isinstance(value, dict) else None


def _cooldown_seconds(config: Any) -> int:
    value = getattr(config, "song_request_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_COOLDOWN_SECONDS
    return max(0, min(MAX_COOLDOWN_SECONDS, value))


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


__all__ = ["SongRequestModule", "SongRequestOutcome"]
