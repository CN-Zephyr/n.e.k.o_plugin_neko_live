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

## Current Viewer Evidence

Ordinary replies about the current viewer's own avatar use the `current_avatar_question` profile. `InteractionRequest.attach_avatar_image` controls image transport independently of the legacy `allow_avatar_image` route hint; the request remains `danmaku_response`, with no extra roast mark. Only self-avatar references opt in, under `avatar_analysis_enabled`; quoted or third-person avatar references do not attach the current viewer's image. Bilibili may resolve that explicitly requested image even when avatar roasting is off. Douyin and Twitch currently return no avatar bytes; this change does not add fetching capability to either provider.

The shared clause classifier removes supported paired ASCII/CJK quotes (including English single quotes with inner contractions) and excludes negated or reported clauses. An explicit refusal to evaluate the current avatar routes even a first-time viewer to ordinary text reply with `avatar_roast_declined`, bypassing both automatic and explicit roast selection without claiming a roast. Bilibili also suppresses that avatar fetch. A separate affirmative clause may still request an image; a negative adjective such as “my avatar looks bad?” does not itself mean refusal. This is a bounded conservative text heuristic, not a general language understanding or coreference service.

The panel's avatar-analysis switch stays editable with avatar roasting off; both settings continue to save independently through the existing config action. No new locale key or UI wording is introduced. The generated compatibility panel carries the same behavior.

Dispatcher binds visual evidence to the actual outgoing parts after image normalization and the existing 64 KiB inline limit. Missing, disabled, default, failed, or over-budget images are unavailable evidence, never permission to describe an older image. It adds an ownership constraint for attached images and avatar questions. The ordinary reply contract permits the current-avatar task instead of prohibiting all avatar descriptions.

`adapters/viewer_evidence.py` inlines a bounded public author label beside current-input text, e.g. `danmaku: 【viewer·v_0123456789ab】utterance`. The short token derives from the normalized viewer UID (SHA256, first 12 hex characters), so truncated or identical names remain distinguishable and a renamed UID keeps its token. It is an identity hint, not a guarantee of UID anonymization or collision-free identity; the prompt marks it as non-spoken and it adds no store or external lookup. The author uses the current public nickname, independently of the preferred address used in the spoken reply. Labels remain untrusted data; they are not spoken reply prefixes. Token fitting may excerpt the utterance but retains the author label. This survives the plugin's own fitting and accompanies text sent into host history; arbitrary host/model summarization is not a lossless attribution channel and cannot be guaranteed by this plugin.

For questions about first visits, meeting dates, or recognition, the required speaker block says to use explicit supplied records only and to admit missing evidence. Earlier model guesses are not evidence. A viewer profile's `first_seen_at` is a local observation timestamp, not proof of first-ever entry or acquaintance. When the current viewer's `ViewerProfile` carries a `first_seen_at` dated before today (local time), the rule replaces the plain "admit unknown" line with that date, framed as "first seen in local records; the real first visit may be earlier". Same-day or unparseable values are withheld, because `ViewerStore` fills a missing timestamp with the load time and a same-day record cannot be distinguished from a fabricated one. The date line only appears for time/recognition questions and replaces the existing rule, so ordinary replies pay no extra tokens. No cross-viewer historical lookup or invented date is introduced. Tests: `tests/test_viewer_evidence.py`.

### Decision Points

- Scope: address the reported missing current image and cross-viewer attribution; use an independent image flag and self-avatar opt-in rather than attaching every viewer image or treating ordinary replies as roasts.
- Budget: reuse already-resolved avatar bytes and existing normalization/64 KiB transport limits. An explicit avatar request may add one current-viewer image and its model vision cost; ordinary replies add none. Bilibili's existing bounded fetch can run for an explicit self-avatar request when roasting is off; other providers add no network work. Relevant image/history boundaries and inline author labels, including one bounded UID hash and its short token, remain inside the 1000-token callback ceiling. Clause scanning is bounded by existing input limits; refusal avoids image fetching and model vision cost. No additional model call, dependency, task, store, or polling loop is added.
- Alternatives and rollback: attachment on every reply costs more and still does not establish who owns a past image. Remove the independent flag consumers and evidence rules together to revert; retain current safety checks and the once-per-viewer roast gate.
- Validation and limits: regression tests alternate two different real images through pipeline and dispatcher, assert current visual identity after re-encoding, keep reply module/roast bookkeeping intact, and cover unavailable/disabled/oversized images, denial/quoted/reported input for new and returning viewers, both modes, long input fitting, long/identical/renamed author names, missing date evidence, and independent panel toggling. These are offline transport/prompt checks, not proof that a live model never hallucinates or that host-compressed history preserves all labels. The host provides no plugin pre-TTS response hook to enforce the final spoken facts.

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
