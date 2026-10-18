# live_presence

`live_presence` owns optional speech for room entry, follow, and likes. `live_audience_session` still owns the counters. `live_support_events` stays on gift, Super Chat, and guard.

Speech switches default off:

- `entry_greet_enabled`: greet by name, routed as `danmaku_response` with the dedicated `entry_greeting` profile, does not mark the viewer roasted. See [entry greeting output contract](output_contract.md#entry-greeting).
- `entry_roast_enabled`: first-appearance roast. This does mark the viewer roasted.
- `follow_greet_enabled`: one thanks, routed as a support event.
- `like_greet_enabled`: batch likes for one idle second, then thank once after `like_greet_min_count` (default 5).
- `like_greet_cross_viewer`: default off. When on, one room burst becomes a single thanks with `like_scope=room` and no viewer name.
- `viewer_context_limit`: same-viewer prompt window, default 6, clamped to 1–12. Hosting and roast still do not continue an old topic. Danmaku replies may answer a follow-up.

`entry_roast_enabled` also sends later text danmaku to `danmaku_response`. With the switch off, the first text danmaku still falls through to avatar roast.

The module never calls `push_message`. Enabled speech goes through `handle_live_payload`, the pipeline, safety guard, and dispatcher.

## Returning viewers

With `roast_once_per_uid` and both entry switches enabled, presence submits the roast candidate as before. The pipeline resolves its final route under the existing per-UID lock: viewers already covered by persisted or session-local roast ownership fall back to `entry_greet` if welcomes are enabled; new viewers retain the roast route. The fallback rewrites `presence_action=greet` and clears synthetic text before request construction, so it receives the dedicated welcome profile and does not consume another roast.

The existing `danmaku_response` exemption already covers welcomes; no broad exemption of entry roasts is added. If welcomes are disabled, duplicate entry roasts remain skipped. Welcome-only entry does not consume the viewer's first-chat roast. Existing module, live-session, permission, Safety Guard, and output cooldown checks still apply: enabling a welcome does not guarantee that every arrival produces speech.

Final routing re-reads the current switches after identity/profile awaits and the UID lock: if the avatar-roast master or entry-roast switch is off, an enabled entry welcome owns the candidate, including for new viewers. If neither action remains eligible, the pipeline records `module_gate: skipped` with `live_presence.disabled`. A queued greet is also skipped when its greet switch is off; it is never promoted to an unrequested roast. Changing a switch can stop a not-yet-dispatched candidate but cannot recall output already accepted by the host.

Follow and like requests also re-check their individual switches at the final pipeline module gate after the same awaits. Disabled candidates record `follow_greet.disabled` or `like_greet.disabled`; the generic support toggle does not override these switches. `clear_pending()` cancels entry/follow speech tasks and like debounce buckets without resetting audience counters. Queue cancellation ownership is defined in [Runtime Observability](../runtime-observability.md#safety-guard).

### Decision Points

- Scope and cost: fix the reported combination of both entry switches and once-per-viewer roasting using the pipeline's existing lookup and UID lock; no new profile lookup, cache, timer, queue, or per-viewer welcome throttle.
- Pending-switch checks and cancellation reuse current tasks and config; no added network request, dependency, model call or periodic work. Tests pause identity and roast-history lookups before disabling follows/likes, then assert no host submission or roast claim.
- Alternative: checking roast history in the synchronous presence handler would require asynchronous work and could race with an in-flight roast. Resolve it at the existing locked route decision instead.
- Rollback and validation: remove the fallback branch and raw-event normalization together to restore prior routing. Tests cover both modes, persisted and session ownership, concurrent same-viewer entry, effective master/presence switch combinations, disabling pending entry during identity or roast-history lookup, welcome disabled, and welcome followed by first chat.
