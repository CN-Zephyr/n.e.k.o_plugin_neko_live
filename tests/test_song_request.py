"""SongRequestModule: matching, permission, cooldown, queueing and jump."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.core.contracts import LiveConfig
from plugin.plugins.neko_live.modules.song_request import (
    ADD_SONG_ENTRY,
    AUTOPLAY_STATUS_ENTRY,
    PLAYLIST_GET_ENTRY,
    PLAYLIST_MOVE_ENTRY,
    SongRequestModule,
)
from plugin.plugins.neko_live.modules.song_request.settings import (
    DEFAULT_TRIGGER_TEXT,
    normalize_cooldown,
    normalize_permission,
    normalize_triggers,
    trigger_tuple,
)
from plugin.sdk.plugin import Err, Ok, SdkError


class FakePlugins:
    """In-memory stand-in for custom_music_list behind ctx.plugin.plugins."""

    def __init__(self, library=None, *, available=True, current_index=None):
        self.library = library if library is not None else {"晴天": "周杰伦"}
        self.available = available
        self.current_index = current_index
        self.songs: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 0

    async def require_enabled(self, plugin_id, *, timeout=5.0):
        return Ok({"enabled": True}) if self.available else Err(SdkError("missing"))

    async def call_entry_json(self, entry_ref, args, *, timeout=10.0):
        self.calls.append((entry_ref, dict(args)))
        if entry_ref == ADD_SONG_ENTRY:
            title = args["title"]
            if title not in self.library:
                return Ok({"output": {"ok": False}, "is_error": True, "error": "SONG_NOT_FOUND"})
            self._next_id += 1
            self.songs.append({"id": f"pl_{self._next_id}", "title": title, "artist": self.library[title]})
            return Ok({"output": {"ok": True, "added_title": title, "added_artist": self.library[title], "playlist_total": len(self.songs)}})
        if entry_ref == PLAYLIST_GET_ENTRY:
            return Ok({"songs": [dict(s) for s in self.songs]})
        if entry_ref == AUTOPLAY_STATUS_ENTRY:
            return Ok({"current_index": self.current_index})
        if entry_ref == PLAYLIST_MOVE_ENTRY:
            item = next(s for s in self.songs if s["id"] == args["item_id"])
            self.songs.remove(item)
            self.songs.insert(args["new_index"], item)
            return Ok({"success": True, "new_index": args["new_index"]})
        return Err(SdkError(f"unknown entry {entry_ref}"))


def _config(**overrides):
    values = {"song_request_enabled": True, "song_request_cooldown_seconds": 30}
    values.update(overrides)
    return LiveConfig.from_mapping(values)


def _ctx(plugins=None, **config):
    return SimpleNamespace(config=_config(**config), plugin=SimpleNamespace(plugins=plugins))


def _event(uid="1001", nickname="观众A", **raw):
    return SimpleNamespace(uid=uid, nickname=nickname, raw=raw)


def _module(ctx):
    module = SongRequestModule()
    module.ctx = ctx
    return module


def test_settings_normalization():
    assert normalize_triggers(" 点歌 \n\n点歌\n来首") == "点歌\n来首"
    assert normalize_triggers(123) == DEFAULT_TRIGGER_TEXT
    assert trigger_tuple("来首\n来一首")[0] == "来一首"
    assert normalize_permission("GUARD") == "guard"
    assert normalize_permission("vip") == "anyone"
    assert normalize_cooldown("9999") == 3600
    assert normalize_cooldown("-5") == 0
    assert normalize_cooldown(True) == 30


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("点歌 晴天", "晴天"),
        ("点歌：晴天", "晴天"),
        ("来一首晴天", "晴天"),
        ("点歌", ""),
    ],
)
def test_match_extracts_query(text, query):
    module = _module(_ctx())
    assert module.match(text) == query


def test_match_ignores_other_text_and_disabled_config():
    assert _module(_ctx()).match("今天天气不错") is None
    assert _module(_ctx(song_request_enabled=False)).match("点歌 晴天") is None
    assert _module(_ctx()).match("点歌 晴天", event_type="gift") is None


async def test_queue_song_success_records_jumpable_row():
    plugins = FakePlugins()
    ctx = _ctx(plugins)
    module = _module(ctx)
    outcome = await module.handle(ctx, _event(), "晴天")
    assert outcome.status == "queued"
    assert (outcome.title, outcome.artist, outcome.playlist_total) == ("晴天", "周杰伦", 1)
    [row] = module.recent_requests()
    assert row["request_id"] == outcome.request_id
    assert row["can_jump"] is True
    assert set(row) >= {"nickname", "query", "title", "artist", "status", "created_at", "jumped"}


async def test_not_found_and_unavailable():
    ctx = _ctx(FakePlugins(), song_request_cooldown_seconds=0)
    module = _module(ctx)
    assert (await module.handle(ctx, _event(), "不存在的歌")).status == "not_found"
    ctx_missing = _ctx(FakePlugins(available=False), song_request_cooldown_seconds=0)
    assert (await _module(ctx_missing).handle(ctx_missing, _event(), "晴天")).status == "unavailable"
    ctx_none = _ctx(None, song_request_cooldown_seconds=0)
    assert (await _module(ctx_none).handle(ctx_none, _event(), "晴天")).status == "unavailable"


async def test_empty_query_does_not_call_music_plugin():
    plugins = FakePlugins()
    ctx = _ctx(plugins)
    outcome = await _module(ctx).handle(ctx, _event(), "")
    assert outcome.status == "empty_query"
    assert plugins.calls == []


async def test_cooldown_per_viewer():
    ctx = _ctx(FakePlugins())
    module = _module(ctx)
    now = [100.0]
    module._clock = lambda: now[0]
    assert (await module.handle(ctx, _event(), "晴天")).status == "queued"
    second = await module.handle(ctx, _event(), "晴天")
    assert second.status == "cooldown"
    assert second.cooldown_remaining == 30
    assert (await module.handle(ctx, _event(uid="2002"), "晴天")).status == "queued"
    now[0] += 30
    assert (await module.handle(ctx, _event(), "晴天")).status == "queued"
    assert len(module.recent_requests()) == 3  # cooldown hits are not recorded


@pytest.mark.parametrize(
    ("permission", "raw", "allowed"),
    [
        ("fans", {}, False),
        ("fans", {"medal_level": 3}, True),
        ("fans", {"guard_level": 3}, True),
        ("guard", {"medal_level": 20}, False),
        ("guard", {"guard_level": 1}, True),
        ("guard", {"guard_level": True}, False),
    ],
)
async def test_permission(permission, raw, allowed):
    plugins = FakePlugins()
    ctx = _ctx(plugins, song_request_permission=permission)
    outcome = await _module(ctx).handle(ctx, _event(**raw), "晴天")
    assert outcome.status == ("queued" if allowed else "denied")


async def test_restricted_permission_denied_off_bilibili():
    ctx = _ctx(FakePlugins(), song_request_permission="fans", live_platform="twitch")
    assert (await _module(ctx).handle(ctx, _event(medal_level=5), "晴天")).status == "denied"


async def test_jump_moves_song_after_current():
    plugins = FakePlugins(library={"A": "x", "B": "y", "C": "z"}, current_index=0)
    ctx = _ctx(plugins, song_request_cooldown_seconds=0)
    module = _module(ctx)
    for title in ("A", "B", "C"):
        await module.handle(ctx, _event(), title)
    c_row = next(r for r in module.recent_requests() if r["query"] == "C")
    result = await module.jump(ctx, c_row["request_id"])
    assert result == {"ok": True, "new_index": 1}
    assert [s["title"] for s in plugins.songs] == ["A", "C", "B"]
    c_row = next(r for r in module.recent_requests() if r["query"] == "C")
    assert c_row["jumped"] is True and c_row["can_jump"] is False


async def test_jump_refuses_unknown_position_and_played_songs():
    plugins = FakePlugins(library={"A": "x", "B": "y"}, current_index=None)
    ctx = _ctx(plugins, song_request_cooldown_seconds=0)
    module = _module(ctx)
    await module.handle(ctx, _event(), "A")
    await module.handle(ctx, _event(), "B")
    a_id = next(r["request_id"] for r in module.recent_requests() if r["query"] == "A")
    assert (await module.jump(ctx, a_id))["reason"] == "current_song_unknown"
    plugins.current_index = 0
    assert (await module.jump(ctx, a_id))["reason"] == "already_played"
    assert (await module.jump(ctx, "sr_missing"))["reason"] == "request_not_queued"
    assert not any(call[0] == PLAYLIST_MOVE_ENTRY for call in plugins.calls)


async def test_jump_when_song_removed_from_playlist():
    plugins = FakePlugins(current_index=0)
    ctx = _ctx(plugins)
    module = _module(ctx)
    outcome = await module.handle(ctx, _event(), "晴天")
    plugins.songs.clear()
    assert (await module.jump(ctx, outcome.request_id))["reason"] == "song_left_playlist"
    assert module.recent_requests()[0]["can_jump"] is False


async def test_duplicate_titles_map_to_distinct_items():
    plugins = FakePlugins(current_index=0)
    ctx = _ctx(plugins, song_request_cooldown_seconds=0)
    module = _module(ctx)
    first = await module.handle(ctx, _event(), "晴天")
    second = await module.handle(ctx, _event(uid="2"), "晴天")
    assert module._queued_items[first.request_id] != module._queued_items[second.request_id]


class _ListOnlyPlugins:
    def __init__(self, result):
        self.result = result

    async def list(self, *, timeout=5.0):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        (Ok([{"plugin_id": "custom_music_list", "status": "running"}]), "ready"),
        (Ok([{"plugin_id": "other_plugin", "status": "running"}]), "not_installed"),
        (Ok([]), "not_installed"),
        (Ok([{"plugin_id": "custom_music_list", "status": "stopped"}]), "not_running"),
        (Ok([{"plugin_id": "custom_music_list", "status": "running", "enabled": False}]), "not_running"),
        (Err(SdkError("query failed")), "unknown"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_music_plugin_state(listed, expected):
    module = _module(_ctx(_ListOnlyPlugins(listed)))
    assert await module.music_plugin_state(module.ctx) == expected


async def test_music_plugin_state_without_plugins_accessor():
    module = _module(_ctx(None))
    assert await module.music_plugin_state(module.ctx) == "unknown"
