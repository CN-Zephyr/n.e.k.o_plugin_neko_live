# Viewer And Security Stores

## Purpose

This store slice keeps viewer profiles, audit events, and encrypted platform credentials behind explicit single-writer boundaries. It adds safe profile projections and maintenance actions, bounded audit records with secret redaction, and isolated credential namespaces for multiple live providers.

## Ownership And Contracts

- `stores/viewer_store.py` is the only writer for `viewer_profiles.json`. It sanitizes loaded records, serializes writes with an async lock, uses atomic replacement, and does not call the memory server. It retains bounded, filtered viewer utterances in impression fields; this is not a full chat archive. `core/viewer_preferences.py` drops presence boilerplate before it becomes an impression: entry/like/follow system lines, connectivity probes (`测试`, `测试一下`, `能不能收到`, `看得到吗`), and platform auto-hints carrying `以后不提示`. Ordinary questions that merely contain `能不能` still count as preferences. Tests: `tests/test_viewer_profile_quality.py`. Longer recall of a viewer's own words goes through `adapters/live_memory_bridge.py` into N.E.K.O `scoped_history`.
- `stores/audit_store.py` is the only audit-event sink. It bounds text, nesting, and list sizes and redacts sensitive text and structured sensitive keys.
- `stores/credential_store.py` owns Fernet-encrypted credential files. The default `bili` namespace preserves legacy filenames while other providers use strictly validated, isolated filenames and field allowlists. Audit identity uses `DedeUserID` for Bili fields and `uid` for provider field sets that expose it; missing identities are recorded explicitly as `unidentified`, never as an empty account id. Missing credential files load silently as logged out; a present-but-unreadable store (wrong key, corrupt file, invalid payload) also loads as logged out but records `{namespace}_credential_load_failed` with only `error_type` (exception class name), so "decrypt failed" is distinguishable from "never logged in" without exposing ciphertext or key material.
- `core/viewer_preferences.py` owns preference inference and the public profile projection returned by `recent_profiles()`.

The prompt memory quote carries an impression and up to two stable favorite-topic tags together. Tags must meet the existing repeat-evidence threshold and belong to the fixed games/music/tech_ai/anime_role allowlist. A nonempty greeting impression therefore cannot hide established interests. Counts remain profile data. Both fields are untrusted quoted history, controlled by `viewer_memory_enabled` and the existing optional-memory budget in [Prompt Delivery](prompt_delivery.md).

Store callers receive plain public dictionaries or contract objects. Viewer mutations require a sanitized provider identity. This identity is part of the existing product contract: `viewer_profiles.json` uses the sanitized value both as the JSON key and as the profile `uid` field; it is not an opaque or cross-provider anonymous identifier. Bili credential audit identity uses `DedeUserID`, while other provider credential fields use an allowlisted `uid` when available. Removing or hashing this persistent profile identity requires a separate schema migration; disabling personalized memory does not rewrite existing identity keys. Credential callers only receive the configured encrypted payload fields.

## Pipeline, Safety, And Data

Viewer data is derived from compact identity and interaction metadata after normal live-event handling. Store operations do not emit NEKO output. Any response using profile guidance still enters `core/pipeline.py`, passes `core/safety_guard.py`, and reaches NEKO only through `adapters/neko_dispatcher.py`.

Viewer profiles contain local nicknames, avatar URLs, counters, compact preference summaries, and recent output summaries. Audit records must never retain cookies, tokens, authorization values, or provider credentials. Credential plaintext exists only in memory during encrypted save/load and must never enter audit, logs, config, or UI.

Personalized viewer memory defaults to enabled. The switch controls local preference and utterance learning, personalized prompt use, and scoped-history collection and submission. Basic identity, interaction counters, first-roast state, and same-session anti-repeat remain active. Profiles expire after 90 days of inactivity and are pruned lazily at startup and during normal store traffic. Local impression fields can retain selected original words. When used in a prompt, stored text is serialized as an untrusted quote governed by the rules in [Prompt Delivery](prompt_delivery.md). Host-memory retention is governed by the host, independently of the local 90-day policy.

## Decision Point: Viewer Memory V1

- **Approved product behavior:** default on; fixed 90-day retention; ordinary-user toggle, clear-all, reset-impression, and delete-profile controls.
- **Cost budget:** bounded local JSON I/O and existing prompt tokens when enabled. The existing scoped-history bridge also sends local HTTP requests to the host memory service; extraction cost is determined by the host. No new model API, dependency, timer, `watch_time`, or `contribution_rank` is introduced by the review fixes.
- **Stable-topic repair:** the approved prompt fix adds at most two existing topic labels to the same bounded memory quote, within the current callback budget. It reuses the current profile projection and adds no storage write or model call. Dropping the optional memory block drops both fields and its usage rules together. Removing the topic field restores the earlier rendering behavior.
- **Action semantics:** reset removes local impressions and preserves identity, counts, and first-roast state; delete removes the complete persistent UID record and its session claim; clear-all affects local profiles and unsent memory batches, not summaries, sandbox results, or safety queues. Delete/reset cancel unsent batches for the UID. None of these actions purges memory already accepted by the host.
- **Rejected alternatives:** disabling the basic profile store with personalization would break first-roast dedup; background retention jobs add idle cost; retaining raw history increases privacy and migration risk.
- **Degrade / rollback.** Disabling `viewer_memory_enabled` stops learning and prompt use. A pending message that has yet to start its disk commit updates basic counts only, even if the switch is quickly re-enabled. The configuration action waits for any commit already started before returning. Previously stored impressions remain available when memory is re-enabled.
- **Evidence:** config projection, disabled-learning, prompt gating, 90-day pruning, reset/delete/clear, panel parity, locale, and full plugin gates cover this decision.

## Profile update performance

The store reads, normalizes and filters the JSON on a worker thread. The worker also refreshes the bounded panel projection. Writes still use an atomic replacement under the writer lock, including when the caller cancels during the commit. The store saves compact JSON with the same schema. Individual updates still scan and rewrite the local file, so measure write latency and event-loop delays separately when testing larger synthetic datasets.

### Decision Points: profile capacity

- **Budget and scope.** Keep the existing JSON writer, schema and 90-day retention, with at most 10,000 records and a 16 MiB UTF-8 file. A stat check rejects an already oversized file; a bounded read also protects against growth after stat. Record count is checked before profile normalization. Encoded output size is checked before creating or replacing any file. No dependency, timer, database migration or network request is added.
- **Admission.** Under the existing writer lock, identity upsert, live learning and roast recording all enforce the same limit. At capacity, existing records can still update within the byte budget. New viewers receive transient profiles; recording their roast returns false. Full capacity does not evict active viewers. Normal expiration or successful explicit deletion can free space. Session anti-repeat remains separate; transient profiles have no cross-restart history guarantee.
- **Preservation.** Existing files within budget remain compatible. A legacy file already over either limit remains untouched and is treated as unavailable, never corrupt: reads return no profiles, learning reports unavailability and maintenance writes refuse to apply. Back up/archive such a file before selecting an in-budget store. A write that would exceed the byte budget leaves the old file and panel cache intact and must not activate a fallback directory.
- **Alternatives and rollback.** Automatic oldest-viewer eviction was rejected because capacity pressure must not erase active profiles. SQLite/incremental persistence is deferred until real write-latency evidence justifies migration. Reverting the limits preserves the schema but restores unbounded resource use; temporarily disabling personalized memory does not disable basic identity storage or these caps.
- **Evidence and observability.** `storage_status()` exposes `profile_limit`, `file_limit_bytes` and `capacity_reasons`; `viewer_store_capacity_reached` contains only `profile_count` or `file_size`, deduplicated for the current store until recovery. Tests in `test_viewer_store_limits.py` cover all write entry points, concurrent admission, expiration, disabled learning, oversized legacy files, bounded reads, UTF-8 bytes, old-file/cache preservation and fallback refusal. Existing store tests cover ownership and cancellation.

## Decision Points for Windows credential protection

- **Protection.** Keep Fernet and the existing namespace files. On Windows, wrap the Fernet key with current-user DPAPI through the system API and the existing worker thread. POSIX keeps separate owner-only key and ciphertext files. A cross-platform keyring would add dependencies and availability requirements.
- **Upgrade budget.** An existing Windows key gets one atomic replacement after its credential decrypts successfully or when the user saves a replacement credential. Normal reads perform one local DPAPI call.
- **Failure and rollback.** If credentials cannot be migrated or unwrapped, the store retains the files and reports the existing redacted load failure. Sign in again after clearing local credentials or retry under the same Windows account. Downgrading to a version that cannot read protected keys requires signing in again.
- **Verification.** Synthetic credentials test Windows wrapping, migration, failed replacement, re-open and logout. POSIX tests retain the mode-bit checks. Both panel surfaces use the same credential actions and public status.

## Local credential removal

- Bilibili, Twitch and Douyin share the logout result contract. `logged_out=true` means both local files are absent and the provider listener stop completed. A missing file is already removed. A partial failure is retryable and produces `credential_removal_failed` or `listener_stop_failed`, without returning an operating-system path or exception message. The action still clears the in-process credential and attempts to stop the provider.
- The store holds its write lock until the worker finishes even if the caller is cancelled. Bilibili invalidates the captured QR session and serializes its final save against logout. Twitch invalidates both the device session and token generation. Douyin serializes explicit imports and removal, and discards late validation results. All listeners continue to use the existing runtime, pipeline and Safety Guard.
- **Decision and cost.** Reuse the store and provider locks on account operations. Removal makes at most two local file-delete calls per attempt. This adds no credential writes, timers or provider requests. After a filesystem error, the running plugin refuses to reload the credential from that store until the next explicit login. Retry logout before restarting if a file could not be removed. This operation is local to NEKO.
- Windows and shared tests cover denied/partial deletion, retry, interrupted disk saves, QR authorization races and provider stop. DPAPI and Job Object tests also run in the Windows CI job.

## Credential request redirects

Bilibili room lookups and listener discovery, Douyin room-page fetches, and Twitch OAuth requests use fixed provider endpoints. These plugin-owned HTTP calls reject redirect responses, including same-origin redirects, before a second request can forward cookies, authorization headers or token form bodies. The two synchronous room lookups share `adapters/http_redirects.py`; asynchronous requests set `allow_redirects=False` and treat 3xx responses as failure. Existing lookup, listener and authorization failure handling remains responsible for reporting the result using sanitized status messages. A provider that starts requiring redirects needs an endpoint update and compatibility verification.

The transport reuses the installed HTTP clients and existing timeouts. It adds no persistent state, polling, model calls or provider requests. `tests/test_http_redirects.py` exercises normal responses and 301/302/303/307/308 with synthetic credentials through the real urllib redirect handlers and local aiohttp servers, including token POST requests and response cleanup. Bilibili QR login and identity queries continue to use the host-provided SDK, whose transport is separate from these plugin-owned requests.

## Testing

Run:

```powershell
uv run pytest plugin/plugins/neko_live/tests/test_viewer_store.py plugin/plugins/neko_live/tests/test_credential_store.py plugin/plugins/neko_live/tests/test_audit_store.py -q
uv run pytest plugin/plugins/neko_live/tests -q --maxfail=1
uv run python -m plugin.neko_plugin_cli.cli check plugin/plugins/neko_live
```

Tests cover JSON persistence and fallback, disabled-memory behavior, 90-day retention, profile reset/delete/clear behavior, prompt gating, encrypted namespace isolation, and redaction of secrets in text and nested structured audit detail.

## Storage And Retry Ownership

- Every profile read/modify/write pins its configured path and fallback ownership
  before the first await. A concurrent `viewer_store_dir` update affects only the
  next operation; an in-flight update and its cache projection retain the old path.
  The ordinary panel still does not expose directory changes. Tests cover both
  runtime updates and host config callbacks, including an existing fallback.
- Scoped-memory retries retain the complete original message batch, speaker
  metadata, idempotency key and captured subject epoch. New chat is buffered
  separately; a lost receipt must not turn old chat into a new request or bypass
  the host's forget fence. This does not change local-profile deletion into a host
  memory deletion. `test_live_memory_retry.py` uses fake HTTP acknowledgements to
  cover response loss, new arrivals, changed epochs, discard and flush.

### Repair Decision Points (2026-11-09)

The user requested repair of the reviewed retry and directory races. The chosen
implementation keeps the existing submission triggers and storage format:

- **Memory budget:** 8 concurrent submissions, 256 pending scopes plus the
  existing in-flight scopes, and at most 12 retained messages per scope including
  its frozen retry. A whole retry takes precedence over fresh chat; overflow
  drops the oldest fresh messages. No timer, polling, dependency or prompt growth.
- **Ordering:** serialize submissions for one scope and try a failed batch only
  once per flush, followed by its fresh tail after success. Merging old and new
  chat would save bookkeeping but breaks request identity, so it is rejected.
- **Storage budget:** reuse the existing store lock with one operation-local path
  snapshot. No additional file reads/writes or config lock are needed; changing
  directories remains available without interrupting an old write.
- **Failure/rollback:** retain the existing bounded retry and unavailable audit
  paths; disable personalized memory to stop new memory submissions. Directory
  changes can be reverted after in-flight operations settle. No on-disk migration
  is introduced. Regression tests must preserve both directories and compare the
  complete retry payload, not only final profile counts.

## Limitations And Rollback

- Viewer profiles are local plugin data and do not provide cross-device synchronization.
- Profile mutations still read and atomically rewrite the canonical JSON file. The bounded dashboard projection removes repeated read/parse work but does not turn JSON into an indexed database; very large 90-day profile sets may still add latency to an individual selected interaction. A storage-engine migration remains a separate decision.
- Ordinary profile updates degrade to the plugin data directory when a configured viewer directory cannot be written. A durable ownership marker binds fallback selection to the configured source so restarts use the latest database. Each custom source has its own hashed fallback data file; an unclaimed custom path never imports or overwrites the ordinary default database. Earlier explicit markers migrate to the separate file on the next write, retaining the shared original for recovery. Old pre-marker fallback files remain untouched and require explicit recovery when their source is unknown. Maintenance writes to that authoritative fallback; marker/write failures fail closed. Restoring the old code requires migrating the fallback back to the configured path first to avoid reading stale data.
- Missing or undecryptable credential files degrade to a logged-out state.
- Rolling back the maintenance APIs leaves existing profile JSON compatible. Rolling back provider namespaces must retain the default `bili_credential.*` files; other namespace files can remain unused without exposing plaintext.


## Scoped Memory Lifecycle

`LiveMemoryBridge` collects only sanitized viewer text from successful live submissions. A `queued_to_neko(...)` receipt is never an assistant turn. The bridge binds target role, provider platform, session generation and UID at collection time. It uses participant subjects such as `twitch:123`, without a second Bilibili prefix. This provides write isolation, not automatic cross-platform identity binding or guaranteed host recall.

`core/viewer_utterance.py` is the shared eligibility rule for local utterance learning/counts and scoped-history collection: authored `live_danmaku` chat includes ordinary danmaku, SC bodies and provider-verified Twitch cheer text. Entry/follow/like, generic gift/subscription descriptions, missing-SC-body fallback labels, synthetic hosting and developer simulation do not add utterances or personal preferences; identity upsert and first-roast bookkeeping remain available. Busy-room batch welcomes learn the original selected danmaku, never the generated welcome intent. This does not erase older contaminated records or memories already accepted by the host.

HTTP success alone is not a memory acknowledgement. The bridge requires a JSON object with `status=processed` and `trust.persisted` exactly `true` or `null`; false, absent/incomplete blocks, numeric lookalikes, and malformed JSON retain the batch for a later flush with the same idempotency key. Successful keys are cached only after this confirmation.

Keyed `scoped_history` writes must carry `subject_epochs` for the participant key (`participant:<platform>:<uid>`) under the default subject scope; without it the host answers 422. Before a batch key's first submission the bridge reads the host fence with one `GET /internal/memory/{target}/forget_epochs?subject=<key>` (absent key means 0) and keeps that value in a bounded per-key cache (512 entries), so every retry of the key resends identical `subject_epochs`. This is one extra local read per new batch, not polling. An unreadable fence (503, transport error, malformed or negative value) never reads as 0: it records `live_memory_unavailable` reason `forget_epochs_unavailable` and keeps the batch for a later flush. A 422 on the write itself means the request can never succeed under that key, so the batch is dropped with reason `memory_request_rejected` instead of retrying forever. Retry uses the existing bounded batches and explicit flush/shutdown lifecycle, with no new timer or network polling. Local text checks and acknowledgement parsing add no model call, dependency or storage schema; disabling personalized memory remains the degrade path. Reverting acknowledgement validation restores the risk of losing unsaved trust/activity updates.

Collection and HTTP submission both check `viewer_memory_enabled`. Disabling discards queued batches and cancels owned requests; it cannot retract requests already accepted by the host. Batches contain six viewer turns. Capacity is bounded to 256 pending scopes, 12 turns per scope, eight owned requests and 1024 successful idempotency keys. Excess pending scopes evict the oldest unsubmitted scope; failed requests retain a bounded tail for a later flush. No periodic retry loop is added. Shutdown stops ingestion first, drains for at most two seconds, then cancels and awaits owned requests. A room disconnect (`disconnect_live_room`) also submits the pending partial batch through the bridge's existing `flush()`, bounded to two seconds with `asyncio.shield`, so a slow host never blocks the disconnect action and the shielded request finishes in the background. A timeout records audit `live_memory_flush_timeout`, a failure records `live_memory_flush_failed` with the exception type only; both leave the batch for the next flush or shutdown. This adds no timer or polling; it moves an existing submission earlier. Tests: `tests/test_runtime_live_controls.py -k disconnect`.

`test_review_memory_lifecycle.py` checks these rules against the real local profile store, ordinary-chat learning after system events, original text after batch welcoming, rejected/malformed HTTP acknowledgements, identical retry keys and confirmed-response dedupe, plus forget-epoch capture, epoch reuse across retries, unreadable fences and permanent 422 rejection. Local identity/profile writes continue through `ViewerStore`; no support ledger is added.

### Decision Points for further memory changes

The current review fixes restore the switch and ownership contract without adding model calls. Local deletion and explicit scoped host erasure are separate actions, with the latter restricted as below. Automatic fact extraction, cross-platform identity linking and independent retention changes require a separate cost/privacy decision. Rollback can disable personalized memory; preserve the fallback ownership marker and authoritative JSON when reverting store code.

## Explicit scoped host erasure

`forget_viewer_memory(uid, platform, target)` is an explicit Hosted UI action in a viewer's profile detail. It is available only after the live connection is stopped. Confirmation freezes the platform, character and UID; the backend checks the current provider/character and stopped state before submission and again before local deletion. A locally stored UID alone is not proof of a platform or character: the operator must check the displayed scope. The operation does not claim to erase other characters, platforms, backups, external services or all operator data.

`adapters/live_memory_forget.py` uses the host's local `scoped_forget` API. It first requires OpenAPI to declare `subject`, `forget_epoch` and `target_epochs`; an older host that silently ignores unknown fields must never receive the erase request. It reads the selected participant's current forget epoch and submits that epoch plus one with exactly one `target_epochs` key. The current host rejects a linked-account expansion with 409 before erasing anything. Such identities belong in host memory management; the plugin does not auto-approve additional accounts. Requests never follow redirects.

The adapter requires the exact `forgotten` subject and single-subject receipt before deleting the selected local profile through `ViewerStore` and clearing its session claim. A generic HTTP 2xx, mismatched receipt, transport failure or local-write failure is not success. The local profile is preserved on unconfirmed host erasure. Audit records use a constant operation description, without response bodies or memory contents.

### Decision Points: erase cost, retry and compatibility

- **Scope/budget:** one serialized user action, at most one schema read, one epoch read and one erase POST per first attempt, all to loopback. Schema/epoch reads have five-second timeouts; erase has a thirty-second timeout. Schema parsing accepts at most 4 MiB. No provider/model calls or background timer is added. The journal reuses the host's existing `filelock` dependency.
- **Durable journal:** `MemoryForgetStore` owns `memory_forget_pending.json` and its `.lock` sidecar under `plugin.data_path()`, with no temporary-directory fallback. At most 64 requests and 64 KiB are accepted; pending requests are never evicted. Each record holds platform, character, selected local UID, original epoch, host port, a hash of the profile-file locations, and phase. It stores no chat, memory text, credentials or raw profile paths. Writes use a same-directory temporary file, flush/fsync and atomic replacement under a one-second file lock. Constructor restoration performs one bounded read; normal incoming messages only consult the cached state.
- **Checkpoint ordering:** `prepared` must reach disk before the erase POST; `host_confirmed` must persist after the exact receipt and before local deletion; `local_deleted` must persist before retiring the request. Cancellation waits for an already-started disk operation before releasing its lock. A retry after an uncertain POST uses the original epoch, including across plugin restart. A confirmed-host checkpoint resumes local cleanup without another host request. A completed-local checkpoint only clears the session claim and retires the journal entry.
- **Retry fence:** pending scopes reject host-memory collection/submission, cancel owned old batches and fence pre-existing epoch reads. Unrelated host platforms/characters sharing a UID are preserved. Local profile writes for that UID and its platform-prefixed alias are conservatively paused while a request remains pending. This prevents recreation between local deletion and checkpoint persistence. An unreadable/unwritable journal pauses all host-memory and ordinary local-profile writes, preserves the file, and exposes `memory_forget.available=false`; it is not silently reset. A definitive 404/409/422 on the first request releases that fresh checkpoint; an uncertain earlier attempt remains pending.
- **Recovery UI/lifetime:** Settings → Privacy lists pending deletions independently of whether the local profile still exists. Restart restores the list and write fences, but never automatically erases anything. Stop live input, restore the original platform/character, host port and profile directory, and confirm the selected pending request. Directory ownership is checked again inside the local store's write lock. After successful completion the fence is removed, so later interactions can create new memory; this is not a permanent exclusion list. Do not manually clear the journal or repoint the host to a different memory dataset while a deletion is unresolved.
- **Alternative/rollback:** local-only deletion remains available and explicitly does not erase accepted host memory. Unsupported or linked-account scopes require host management. Resolve pending requests before downgrading to a plugin version without the journal; retain the file if recovery is needed. This design assumes one active plugin runtime; the file lock prevents lost journal updates between instances, but does not provide distributed live-runtime coordination.
- **Tests:** `test_memory_forget_store.py` and `test_live_memory_forget.py` cover bounded/corrupt journals, failed replacement, cancellation, restart, frozen epochs, phase-only cleanup, aliases, directory changes and local write blocking. UI checks cover restored pending rows without automatic execution, stopped-state gating, frozen confirmation and retry after failure.

### Isolated host contract check

Run `uv run python plugin/plugins/neko_live/tools/check_memory_forget_host.py` from the host project. It mounts the actual typed erase/epoch routes and generated OpenAPI in an in-process ASGI application, using real fact, persona, reflection, dedup, tombstone and local-profile persistence with synthetic data. The script installs isolated configuration/log/temp roots before host singleton imports, rejects writes outside its checked temporary directory, blocks external connections and closes logs before cleanup. No host service or model is started.

The 2026-11-15 check passed three host POSTs: a lost receipt followed by restart/replay preserved a newer fact, and a separate confirmed deletion followed by local failure resumed locally without a fourth POST. Other viewer/platform/character scopes and legacy private facts survived. This verifies the route/storage contract; it does not exercise deployed middleware/authentication, a running production host, real user-memory deletion, or Electron/audio acceptance.

## Capacity feedback and synthetic measurement

Settings → Privacy treats nonempty `capacity_reasons` as a warning even when the existing file is writable. `profile_count` explains transient new viewers; `file_size` explains unsaved updates. Legacy oversized files remain preserved under the capacity rules above.

Run `uv run python plugin/plugins/neko_live/tools/benchmark_viewer_store.py` from the host project to measure actual `record_live_danmaku()` disk updates and five-millisecond event-loop sampling. The script only creates synthetic profiles in a checked workspace temporary directory, verifies persisted counters and removes that directory when done. `--profiles 10000 --rich` fills bounded history fields near the byte limit. It does not touch configured user stores, perform network calls or prove live stream throughput.

The 2026-11-15 Windows / Python 3.11.15 baseline used 20 measured updates after one warmup:

| Synthetic profiles | File bytes | Update P50 / P95 | Maximum extra loop delay |
|---|---:|---:|---:|
| 1,000, minimal fields | 444,949 | 45.51 / 51.98 ms | 32.64 ms |
| 10,000, minimal fields | 4,467,949 | 336.97 / 356.33 ms | 40.55 ms |
| 10,000, populated history fields | 15,567,378 | 738.11 / 783.62 ms | 50.61 ms |

These measurements describe this machine and fixture, not a performance target or concurrency guarantee. JSON still scans and rewrites the full file; a pipeline event may perform more than one store update. SQLite or incremental persistence remains a separate schema/cancellation/ownership migration, to be justified against the real deployment workload.
