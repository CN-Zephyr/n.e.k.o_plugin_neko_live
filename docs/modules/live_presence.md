# live_presence

`live_presence` owns optional speech for room entry, follow, and likes. `live_audience_session` still owns the counters. `live_support_events` stays on gift, Super Chat, and guard.

Speech switches default off:

- `entry_greet_enabled`: greet by name, routed as `danmaku_response`, does not mark the viewer roasted.
- `entry_roast_enabled`: first-appearance roast. This does mark the viewer roasted.
- `follow_greet_enabled`: one thanks, routed as a support event.
- `like_greet_enabled`: batch likes for one idle second, then thank once after `like_greet_min_count` (default 5).
- `like_greet_cross_viewer`: default off. When on, one room burst becomes a single thanks with `like_scope=room` and no viewer name.
- `viewer_context_limit`: same-viewer prompt window, default 6, clamped to 1–12. Hosting and roast still do not continue an old topic. Danmaku replies may answer a follow-up.

`entry_roast_enabled` also sends later text danmaku to `danmaku_response`. With the switch off, the first text danmaku still falls through to avatar roast.

The module never calls `push_message`. Enabled speech goes through `handle_live_payload`, the pipeline, safety guard, and dispatcher.
