# Danmaku Filter

`modules/danmaku_filter/` owns the configurable opt-out rule for ordinary live chat. After leading whitespace is removed, a message beginning with a configured literal prefix is skipped as a whole. Matching is case-sensitive; a prefix in the middle or at the end does not opt out. Defaults are `!` and `！`. Gift, SC, guard, follow, like, and entry events keep their existing behavior.

## User settings

The independent **Chat filter** card under live interaction has an enable switch and a settings dialog. Enter one prefix per line; saving applies the rules immediately and persists them through the existing `update_config` path. Restoring defaults edits the draft only until Save is clicked. Failed saves preserve the draft. The same modular panel and generated `panel_compat.tsx` serve supported hosted contexts; all new labels are translated in eight locales.

- `danmaku_filter_enabled`: defaults to true. Turning it off preserves the saved prefix list.
- `danmaku_filter_prefixes`: canonical newline-separated text, default `!\n！`. The API also accepts a list of strings and persists normalized text. CR/LF separates rules; within each rule and incoming message, consecutive Unicode whitespace is folded to one ASCII space, matching live text projections. Leading/trailing whitespace and empty/duplicate entries are removed. An explicitly empty list filters nothing.
- Up to 32 unique prefixes of 64 Unicode characters each; no regular expressions. Explicit invalid or oversized updates fail before runtime mutation or persistence. The settings input has an 8192-character bound. Config loading tolerates old/malformed data: missing or invalid types use defaults, overlong entries are discarded without shortening them into broader matches, and only the first 32 valid unique entries are retained.

## Interfaces and lifecycle

`DanmakuFilterModule` is registered as a default-enabled interaction module. Its `reason(text, event_type=..., config=...)` method returns `input.ignored_prefix` or an empty string. It compiles at most one bounded prefix tuple and refreshes it when config changes. `skip_reason(ctx, ...)` and `viewer_event_skip_reason(ctx, event)` share the same module decision at provider-neutral and normalized event boundaries. Registry disable and the persisted config switch both stop filtering. `settings.py` owns normalization for config contracts and strict update validation.

The module has no event subscription, task, timer, queue, or store. A separate EventBus subscriber could not stop sibling subscribers, so existing input consumers explicitly check this synchronous policy before using message content:

- `live_events.observe_danmaku()` checks before recent-chat storage, topic collection, ballot answers, ambient-context refresh, reply selection, or cooldown reservation.
- `runtime_live_input.handle_live_payload()` checks before updating activity state or observing direct live input, then hands the event to the same pipeline guard.
- `LivePipeline.handle_event()` rejects ignored live, manual-simulation, and developer-sandbox chat before identity/avatar resolution, profile access, Safety Guard queue admission, request construction, or Dispatcher. Unfiltered events retain the existing permission / Safety Guard / Dispatcher path.

Filtered content is not included in recent results, viewer conversation memory, or model prompts. Only the stable reason and existing privacy-safe metadata enter the bounded audit / timeline at `input_filter`, outcome `skipped`. No raw filtered text is logged. Provider receive counts and audience participation counters may still count a received chat event; they do not retain its text. This rule controls this plugin only, not platform visibility, host microphone input, or other plugins.

## Decision Points

- Approved scope: the maintainer requested a separate module and then user customization. Add a persisted enable switch, literal prefix list, independent card/dialog, default restoration, and eight-locale text; retain leading-whitespace handling.
- Budget: one synchronous local decision per input boundary, folding whitespace in the existing bounded text and comparing at most 32 prefixes of 64 characters each. One cached source string/tuple per module, bounded persisted text, and one existing config-save request per explicit Save; no requests on each keystroke, extra dependencies, background work, model calls, or prompt tokens. Ignored input avoids downstream processing and storage; diagnostics reuse existing bounded audit / timeline and runtime log aggregation.
- Alternatives: a reply-only filter would let ignored text affect room topics and passive context. An EventBus redesign would widen this change. Use one policy module checked by existing observation and pipeline entry points.
- Rollout / rollback: old profiles receive enabled defaults; new settings persist across reloads and apply without listener restart. Toggle off to restore ordinary processing, preserving the list. Revert registration and call sites together to remove the module; ignored messages are not replayed. A missing/reserved module is reported by the existing registry and does not invent a replacement capability.
- Verification: cover normalization and bounds, literal/case-sensitive matching, empty lists, defaults, hot updates and persistence/reload; prefix-only messages, repeated spaces, tabs, Unicode whitespace without accidental rule splitting, punctuation elsewhere, both live modes, actual Bilibili / Douyin / Twitch normalizers, direct/sandbox ingress, disable/enable, support-event exceptions, no context/profile/memory/output side effects, and privacy-safe skip diagnostics. The real hosted UI-kit harness verifies editing, toggling, failed/successful save, whitespace folding/deduplication and restoring defaults. Run the full plugin pytest suite, UI checks, and CLI check.

## Tests and limits

From the host project directory:

```powershell
uv run pytest ../n.e.k.o_plugin_neko_live/tests/test_danmaku_filter.py -q
uv run pytest ../n.e.k.o_plugin_neko_live/tests -q
uv run python -m plugin.neko_plugin_cli.cli check plugin/plugins/neko_live
```

The real repository path in the pytest command avoids a Windows junction-path self-scan in the existing plugin identity test. These offline checks verify routing and data exclusion, not live platform transport or audible playback. Historical context already collected before upgrade is not retroactively deleted.
