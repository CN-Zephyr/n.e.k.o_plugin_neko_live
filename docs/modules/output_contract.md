# Output Contract And Danmaku Response

## Purpose

The output-contract slice keeps NEKO Live speech short, attributable, and safe before the host transports it. It also owns normal danmaku request classification and prompt construction.

## Ownership And Contracts

- `modules/danmaku_response/module.py` classifies the current public danmaku and builds an `InteractionRequest`; `core/danmaku_text_rules.py` owns its small reaction and mention classifiers without depending on the later active-topic slice.
- `adapters/output_contract_bridge.py` maps that request to plugin-owned reply metadata.
- `core/live_reply_contract.py` declares route limits and reply modes.
- `core/live_output_contract_prompt.py`, `core/live_output_quality.py`, and `core/live_output_shape.py` render, validate, and shape the final spoken line.
- `adapters/neko_dispatcher.py` is the only output boundary. The host receives opaque metadata and does not own NEKO Live wording policy.

## Data Flow And Safety

Live and sandbox input still enter `core/pipeline.py`. The pipeline applies the permission gate and `core/safety_guard.py` before dispatch. Normal danmaku responses read the current public event, sanitized viewer profile context, recent plugin output, and the configured live theme. They do not write a new store and do not bypass the audit, pipeline, or dispatcher boundaries.

Viewer-supplied nicknames, named targets, and short danmaku anchors may be interpolated into the output contract so the reply remains attributable. They are always marked as untrusted public data, never instructions; embedded requests to change rules, reveal context, or perform actions must not be followed.

The pure shaper can remove stage directions and internal-context leaks, reject unsafe or unfulfilled reply shapes, enforce a route character ceiling, and record shaping reasons in plugin metadata. The current host SDK does not expose the generated reply to the plugin before TTS, so live delivery currently relies on the injected prompt contract and opaque metadata; hard post-generation shaping is reserved for a future generic host callback. Hosting coalescing uses the target, hosting source, and stable beat identifier so duplicate delivery can collapse without merging different beats.

## Testing

Run:

```powershell
uv run pytest plugin/plugins/neko_live/tests/test_output_contract.py -q
uv run pytest plugin/plugins/neko_live/tests -q --maxfail=1
uv run python -m plugin.neko_plugin_cli.cli check plugin/plugins/neko_live
```

Coverage includes reply length, fulfilled content requests, hosting coalescing, named-target parsing, and adversarial viewer fields remaining data rather than control instructions.

## Limitations And Degrade Behavior

- Reply quality checks are deterministic heuristics; uncertain text falls back to a short safe line.
- Generic words are not accepted as named roast targets. A public nickname or explicit mention is required.
- The host currently treats output-contract metadata as opaque transport metadata and provides no plugin-owned post-generation or pre-TTS transform hook. Character ceilings are therefore prompt-level best effort on the live delivery path until that generic host capability exists.

To roll back this slice, remove the output-contract bridge from the dispatcher and restore the previous danmaku module registration. The EventBus, pipeline, safety guard, and viewer stores remain compatible because their public contracts are unchanged.

## Live Context Digest

### Problem

The host truncates each `proactive.callback` to `AGENT_CALLBACK_TEXT_MAX_TOKENS` (1000) and keeps the head. The earlier long English `prompt_text` pushed the viewer line past the cut, so normal danmaku delivery was replaced by `_short_live_danmaku_delivery` (about 230 tokens). That fixed truncation but dropped every context block, and its "standing style lives in the opening read" assumption does not hold: the opening charter is a passive `read` callback, and the host only drains passive callbacks when the owner types or a voice session hot-swaps, so during a live stream the model usually never sees it. Replies lost theme, anti-repetition, same-viewer continuity, room topic, and memes.

### Design

- Head stays first and unchanged: current danmaku, nickname rule, profile note, mode line.
- `modules/_prompt_context_digest.py` renders a Chinese digest of the existing context providers, one or two lines per block (the `theme` block also carries a one-line `roast_strength` tone hint), in fixed priority: `theme` → `recent` → `room` (live_events block, else room danmaku) → `viewer` → `meme` → `preference`. The first line marks the digest as untrusted public data, not instructions.
- Budget: `LIVE_CONTEXT_DIGEST_MAX_CHARS` over the whole digest. A block that does not fit is dropped whole, never cut, matching `live_events._fit_context_blocks_with_indexes`.
- `DanmakuResponseModule.build_request` fills a new `InteractionRequest.delivery_context` field. It is not part of `to_public_dict()`, so it never reaches audit or UI.
- `push_roast` sends `head + "\n\n" + delivery_context` for normal danmaku. The every-8-turns reminder and its "按开场说明" line are removed, because the theme anchor now rides every turn.
- Forced replies, avatar roasts, and other routes are unchanged.

### Decision Points (approved: recommended options)

1. Token cost per danmaku turn. Today: about 230 tokens. Before the short head: about 1000+ (truncated). Proposed: head plus digest at most 600 chars (about 480 tokens), total about 700, under the 1000 host cap with room for host wrapping. Alternatives: 400 chars (about 550 total, meme/preference usually dropped) or 800 chars (about 870 total, little margin). Recommended: 600.
2. Block priority. Recommended order above. Alternative: put `viewer` before `room` for co-streams where regulars dominate.
3. Theme anchor frequency. Recommended: every turn (two to three lines). Alternative: keep the opening read and resend the anchor every N turns, which saves about 60 tokens per turn but leaves most turns without theme.
4. Opening charter. Recommended: keep it as a passive read for owner-typed turns; do not rely on it for danmaku. Alternative: change it to an active callback, which triggers an unwanted spoken reply at stream start.
5. Rollout and rollback. No new config switch by default; rollback is reverting the digest call so `delivery_context` stays empty and the head-only path is used. Alternative: add a `live_context_digest_enabled` config field (needs 8-locale UI text).

### Tests

`tests/test_live_context_digest.py` covers priority order, whole-block drop under budget, empty digest, theme anchor and spent material reaching the request, non-export of `delivery_context`, head-then-digest order with viewer line and theme kept inside `truncate_to_tokens(text, 1000)`, head-only fallback, and removal of the mid-stream reminder, plus the tone hint. `test_danmaku_response_prompt_uses_wider_recent_context_window_by_default` now expects the digest's second recent-context call (limit 4). `test_live_reply_reminder_rides_every_eighth_danmaku` in `tests/test_dispatcher_contracts.py` is removed with the reminder.

No new store, timer, network call, or dependency. Output still goes through `core/pipeline.py`, `core/safety_guard.py`, and `adapters/neko_dispatcher.py`.
