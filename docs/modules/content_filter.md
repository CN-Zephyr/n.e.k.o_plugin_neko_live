# Text exclusions and viewer admission

`modules/content_filter` owns a bounded, streamer-configured literal word list. It checks input/submission text and requests generated-text enforcement from compatible hosts; it is not semantic moderation. An empty `content_filter_terms` disables it. Settings → Privacy edits the list explicitly; drafts are not saved on each keystroke. All eight locales describe its limits.

## Contracts and boundaries

- `core/contracts_config.py` normalizes configuration; `clean_config_updates()` rejects invalid saves. At most 128 distinct terms, 64 normalized characters per term and 8,192 source characters are accepted. Matching uses Unicode NFKC, case folding, removal of format characters and folded whitespace. Terms are literal substrings, never regular expressions; broad terms can block legitimate requests.
- `danmaku_filter.viewer_event_skip_reason()` checks the supplied nickname and text before pipeline identity/profile work, independently of the prefix-filter toggle. `LiveEventsModule.observe_danmaku()` also checks before recent-chat/topic collection. Blocked observations do not become response candidates.
- `core/pipeline_dispatch.py` checks the prepared prompt and resolved identity again after asynchronous host reads, before output cooldown is consumed. `adapters/neko_dispatcher.py` checks actual outgoing text after contracts/history are appended. Scene context, ambient context and developer announcements use the same check. A late dispatcher rejection may already have consumed the normal output cooldown.
- Context-expiration tombstones remain deliverable so an existing host snapshot can be cleared. Existing snapshots are not retroactively scrubbed when a term is added. Existing stored profiles are not automatically deleted.
- Ordinary accepted requests still pass the permission gate, pipeline, Safety Guard and dispatcher. Word lists add no alternate output path or profile/audit writer. Audit/timeline reasons never include the matching term or raw input.

## Generated replies on a compatible host

For `ai_behavior="respond"`, the dispatcher adds a transport-only snapshot:
`response_text_policy={version:1,blocked_terms:[...]}`. It is absent for an empty
list, passive context and expiry tombstones. The snapshot does not enter the
plugin's request/result diagnostic metadata and is not prompt text.

The paired host implementation (`main_logic/response_text_policy.py`, documented
in the host's `docs/design/response-text-policy.md`) buffers a complete proactive
text reply before the normal display/TTS callback. Terms spanning chunks or tool
rounds are checked together. Rejection retires the callback instead of repeatedly
regenerating it. Cancellation, failure and mode changes cannot release the
unchecked buffer. Ordinary chat is not governed by this per-request contract.

The host also checks its display/TTS-cleaned text: removing emotion tags,
Markdown, bracketed asides or symbols can create a term absent from the original
reply. Both TTS space-normalization modes are checked to cover provider fallback;
approved text retains its original formatting. More than 256 format markers
is a terminal `output_policy.too_complex` rejection before formatter work.
See the host contract for the exact marker set. These checks remain literal,
not semantic moderation or a model of arbitrary remote speech transformations.

This requires **both host and plugin updates**. Older hosts ignore the metadata
and retain input/submission checks only; SDK submission acceptance is not a
capability receipt. Direct realtime audio cannot be checked at this text
boundary, so the compatible host rejects policy-bearing replies in that mode.
The queued policy is frozen at submission; changing settings affects new requests.

## Viewer admission

`core/safety_guard_input.py` owns a sliding window at `SafetyGuard.before_event()`: a viewer can enter ordinary live/manual-chat processing five times per ten seconds. Platform, room and UID form the bucket key. The limiter runs before identity/avatar requests and profile I/O; it does not throttle cheap provider receipt statistics or event selection. Gift, SC, guard, presence, hosting and offline developer-sandbox sources retain their existing controls.

At most 4,096 active viewer buckets are held, each with at most five monotonic timestamps. Once full, new identities are rejected until buckets expire; active buckets are not evicted by UID rotation. Disconnect clears the buckets. Clearing the output queue or resuming safety does not refill a viewer's window. `safety.viewer_input_rejected` is a process-lifetime count, not a viewer identifier.

## Decision Points

| Decision | Selected scope, budget and alternative |
|---|---|
| Moderation cost | Reuse literal text checks; no extra model call, network request, dependency or periodic task. Cache one normalized list; reject checked values beyond 32,768 characters. On compatible hosts, opt-in replies wait until complete before speech, retaining at most 32,768 characters/chunks and 128 terms per invocation. Semantic and image moderation remain outside this slice. |
| Host interface | Generic v1 metadata carries at most 128 terms / 8,192 characters. The host uses their union for a combined callback batch under the same bound; an oversized union is rejected. Full buffering catches cross-chunk terms at the cost of first-audio latency. Up to five bounded display/TTS views reuse existing cleanup, with a 256-format-marker cap before parsing. Checking raw text alone misses cleanup-created terms; unbounded markup parsing can stall the event loop. |
| Flood cost | Five timestamps × at most 4,096 scopes, lazily expired. This protects expensive admitted work without introducing another event queue. A transport-wide or distributed limiter is outside this slice. |
| Settings and rollout | An empty list disables literal exclusions; the independent [reply transform](output_contract.md#post-generation-enforcement-status) can still buffer and shape a reply. Update and validate both host processes before claiming generated-text enforcement. Clear the list to disable exclusions; reverting the host removes reply protection. The admission limiter and support routing remain independent. No data migration is needed. |
| Verification | `test_content_filter.py`, `test_viewer_input_limiter.py`, dispatcher regressions and `tools/check_panel.mjs` cover normalization, drafts, failed saves, async setting changes, final appended text, limits and support bypass. |

## Limits

Literal matching does not understand intent, obfuscations outside the bounded cleanup checks, images/OCR or speech audio. The paired host can reject generated proactive text before display/TTS and repeats the checks after the plugin's route-specific shaper. This is not comprehensive final-output moderation or a guarantee about audible playback. Older hosts and native audio retain the shaping limits described in [output contract](output_contract.md#post-generation-enforcement-status). See [runtime observability](../runtime-observability.md) for diagnostics.
