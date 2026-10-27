# Song Request

`modules/song_request/` lets live viewers queue songs in the `custom_music_list` plugin with a trigger danmaku such as `点歌 晴天`. The module only matches, checks permission and cooldown, and calls the music plugin; the spoken reply goes through the normal request builder, Safety Guard and Dispatcher.

## User settings

The **Song request** card under live interaction has an enable switch and settings for triggers, permission and cooldown. Labels are translated in eight locales; `panel_compat.tsx` carries the same card.

- `song_request_enabled`: defaults to false. Off means trigger danmaku follows the ordinary reply path.
- `song_request_triggers`: newline-separated literal prefixes, default `点歌 / 我想听 / 来一首 / 来首 / 放一首 / 放首 / 求歌`. Whitespace is folded with the `danmaku_filter` normalizer; up to 32 unique triggers of 32 characters each, total text bounded to 2048 characters. Invalid input falls back to defaults. Longer triggers are matched first, so `来一首` wins over `来首`.
- `song_request_permission`: `anyone` (default), `fans` (any Bilibili fan medal or captain tier) or `guard` (captain tier only). Medal and guard levels come from the normalized Bilibili event `raw`. Other platforms do not expose these fields, so only `anyone` admits them.
- `song_request_cooldown_seconds`: per-UID cooldown, default 30, clamped to 0–3600; 0 disables it.

## Flow and interfaces

1. `live_events.submit()` forwards a matching danmaku immediately instead of buffering it in the cooldown selection window, and does not advance its local cooldown. Disabled or non-matching text keeps normal selection.
2. `LivePipeline` calls `core/pipeline_flow._maybe_handle_song_request()` for live danmaku with text. If the module is missing, reserved or disabled, or the text matches no trigger, the event keeps its normal route.
3. `SongRequestModule.match()` returns the query after the trigger (separators ` :：,，、` stripped, at most 64 characters), `""` for a bare trigger, or `None`.
4. `SongRequestModule.handle()` never raises and returns a `SongRequestOutcome`:
   - `cooldown`: the UID asked again too soon. The pipeline records `song_request.cooldown` at stage `song_request`, outcome `skipped`, and sends no reply.
   - `denied` / `empty_query`: replied to, and the cooldown starts so a viewer cannot spam replies.
   - `unavailable`: `custom_music_list` is not installed or not enabled (`ctx.plugin.plugins.require_enabled`).
   - `queued` / `not_found` / `error`: result of the music plugin's `add_song_to_playlist` LLM-tool entry. `SONG_NOT_FOUND` maps to `not_found`.
   - `dry_run`: when `dry_run=true`, the `require_enabled` availability check still runs, but `add_song_to_playlist` is never called. The reply only acknowledges the request and the row cannot be jumped, so no-sound chain checks leave the playlist untouched.
5. Every non-cooldown outcome becomes an `InteractionRequest` from `build_song_request_reply()` with prompt rules in `modules/_prompt_song_request.py`, then goes through `dispatch_routed_request()`; it does not mark avatar roast as sent.

Cross-plugin calls use `ctx.plugin.plugins.call_entry_json()` with timeouts (15 s for adding, 5 s otherwise). `add_song_to_playlist` does not return the new item id, so after adding the module reads `playlist_get` and picks the newest untracked item with the same public title and artist. Adding and locating run under one lock so concurrent requests cannot claim each other's item.

### Panel entries

- `song_request_status`: read-only; returns up to 50 recent requests, newest first, with `can_jump`, plus `music_plugin_state` (`ready` / `not_installed` / `not_running` / `unknown`, from one `plugins.list()` call). The panel calls it silently once when it opens. On `not_installed` the Song Request card is grayed out, its toggle is locked, and hovering or keyboard focus shows a centered `role="tooltip"` overlay (styled with the panel's `--surface` / `--border` / `--text` tokens) asking the user to install `custom_music_list`. Every other state keeps the card usable, and runtime failures still surface as `music_plugin_unavailable`.
- `song_request_jump(request_id)`: moves a queued song to the slot right after the song now playing, via `playlist_autoplay_status.current_index` and `playlist_move`. It refuses with a stable reason when the request is not queued (`request_not_queued`), the music plugin is unreachable (`music_plugin_unavailable`), the song left the playlist (`song_left_playlist`), the current song is unknown because autoplay is not running (`current_song_unknown`), the song is playing or already played (`already_played`), or the move fails (`move_failed`). A successful jump marks the row `jumped` and cannot be repeated.

## Data and privacy

State is in memory only: a 50-row recent list, a per-UID cooldown map bounded to 1024 entries (expired entries are pruned when full), and request-id to playlist-item-id links for rows still in the recent list. Nothing is written to viewer store, audit store or disk by this module. Rows hold UID, nickname, query, matched title/artist, status and a UTC timestamp, all passed through `public_text`. No raw payload, cookie or token is read or logged. Restarting the plugin clears the list and cooldowns; songs already added stay in the music plugin's playlist.

## Decision Points

- Scope: the maintainer asked for danmaku song requests that reuse `custom_music_list` rather than a new player. Approved: trigger list, three permission tiers, per-UID cooldown, LLM reply, and a panel list with "play next".
- Budget: one synchronous prefix match per live danmaku when enabled; per accepted request one `require_enabled` check, one add call and one playlist read; per jump one status read, one playlist read and at most one move. No timers, polling, background tasks or new dependencies. Memory is bounded as above. Each reply uses one ordinary reply prompt.
- Alternatives: calling the music plugin's HTTP API or reading its files would bypass its plugin boundary. Inserting directly at a fixed index would ignore the song now playing. Use plugin entries and the reported `current_index`.
- Rollout / rollback: off by default. Turning it off restores the ordinary reply route without restart. Removing the module registration makes the pipeline skip the hook because `match` / `handle` are missing.
- Verification: see Tests.

## Tests and limits

From the host project directory:

```powershell
uv run pytest ../n.e.k.o_plugin_neko_live/tests/test_song_request.py ../n.e.k.o_plugin_neko_live/tests/test_song_request_pipeline.py -q
uv run pytest ../n.e.k.o_plugin_neko_live/tests -q
uv run python -m plugin.neko_plugin_cli.cli check plugin/plugins/neko_live
```

Tests use a fake cross-plugin accessor and cover trigger matching, permission tiers, cooldown, music-plugin errors, `dry_run` skipping the add call, item location under concurrency, jump refusals, the pipeline route, and the `live_events` window bypass (`tests/test_live_events.py`). They do not exercise a real `custom_music_list` process or audible playback.

Known limits: the song is queued before output, so when the Safety Guard output cooldown is still active the song is added but the spoken reply is skipped as `rate limited`. the jump needs the music plugin's autoplay to be running, since that is the only place it reports the current index. If a viewer requests a song identical to an untracked one already at the end of the playlist, the locator may pick that one; the jump then moves an identical song.
