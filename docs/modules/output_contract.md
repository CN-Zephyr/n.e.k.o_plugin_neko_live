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

## Entry Greeting

The presence handler emits `event_type=entry`, `presence_action=greet`, and empty `danmaku_text`: arriving in the room is not a viewer-authored message. The response module classifies this pair as `entry_greeting` before ordinary text classification. Legacy payloads containing the synthetic text `来了` receive the same classification, but that synthetic text is not included as speech to answer.

The request targets `arriving_viewer` with `one_named_welcome_no_thanks`. Its rules require a short named welcome and explicitly distinguish arrival from gifts, follows, likes, or paid support. Earlier room talk, viewer conversation, preference callbacks, and room-topic material are omitted from this greeting so an old thank-you or question cannot become its current task. The route-specific output contract also renders a welcome instruction instead of the ordinary “answer the current danmaku” instruction. Real support events retain their existing thanks route.

No new UI, locale key, event subscriber, store, or host hook is introduced. Live presence still goes through normalization, pipeline, permission checks, Safety Guard, and Dispatcher. Existing metadata carries the new profile/target/shape for inspection. This fixes an ambiguous input contract; it does not prove the streamer's exact model output has been reproduced or provide post-generation enforcement before TTS.

### Decision Points

- Scope: the maintainer requested fixing entry welcomes becoming thanks first; this slice only separates entry-greeting semantics. Exclamation-prefix filtering belongs to the separate [danmaku_filter module](danmaku_filter.md).
- Budget: replace synthetic danmaku interpretation with one arrival profile and explicit welcome constraints. No extra model calls, dependencies, timers, queues, or stored conversation turns; Dispatcher keeps the existing 1000-token ceiling and rejects an unfit required contract. Removing irrelevant greeting context reduces its optional token use.
- Alternative: changing only the wording of the old greeting rule would retain the `short_line` profile and the conflicting generic danmaku contract. The dedicated profile is recommended because it preserves the same event meaning through metadata and delivery.
- Rollback: revert this profile, presence payload, and route-note change together; no configuration or stored-data migration is required.
- Validation: `test_live_presence.py` checks the empty entry payload and unchanged route/roast bookkeeping. `test_prompt_delivery_dispatcher.py` checks both live modes, legacy entry text, repeated delivery, oversized optional context, and separation from chat/support events. Actual model wording and audible playback still need streamer acceptance testing.

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
