# Maintained Hermes fork

This repository tracks `NousResearch/hermes-agent` while carrying a small set of Brian-owned patches. Official upstream remains authoritative for all unmodified Hermes code. Fork `main` is the last candidate that passed fork verification; runtime promotion is a separate operation.

## Non-negotiable patch lifecycle

Every Brian-owned core patch must have an active entry in this file **before it is published**. The entry must name its stable commit subject, summarize the behavior, identify upstream tracking, name regression evidence, and give a source-level rollback procedure. A patch is not complete merely because its commit appears in Git history. Retired entries remain in this file as historical lifecycle records even though their private code must be gone.

When official upstream releases behavior that satisfies a patch contract, the private implementation must be **completely retired in favor of upstream**. Do not keep both implementations, a compatibility shim, disabled private code, or duplicate fork-specific tests “just in case.” Inspect the upstream implementation, run this entry's regressions against it, remove the private code, adapt or delete duplicate tests, promote the upstream-backed candidate across every active runtime, and verify the behavior there. Git history is the rollback record.

Record the removal commit's stable subject on the `Retired` row so fork-only code history remains attributable. An upstream issue, pull request, merge, or similar-looking commit is not enough. Retirement requires equivalent released behavior proven against the patch contract. If upstream only partially covers the contract, narrow and re-document the remaining private patch rather than claiming retirement.

Stable commit subjects survive rebases and are the manifest keys. Resolve the current SHA from the fetched fork history instead of persisting a value that the next upstream rebase will invalidate.

## Plugin overlap and retirement

Every upstream reconciliation must inventory the plugins currently installed or enabled across the maintained Hermes profiles and compare the problem each plugin solves with newly released upstream behavior. Use the live profile-aware Hermes plugin/configuration surfaces and the canonical source owner for each plugin; do not rely on a stale list copied into this file. Inspect released behavior and tests, not issue titles, open pull requests, or feature names.

When released upstream provides a native solution that significantly overlaps with a plugin and satisfies the plugin's actual problem contract, prefer the native solution and **completely retire the plugin**. Do not keep both implementations, leave the plugin disabled, preserve a compatibility shim, retain plugin-owned hooks or schedules, or keep duplicate tests “just in case.” Verify the native replacement against the plugin's real regressions in an isolated candidate, then during the separately authorized runtime promotion disable and uninstall the plugin from every affected profile, delete any Brian-owned canonical plugin source that is no longer used, remove plugin-owned configuration, skills, dependencies, schedules, and generated copies, and prove only the native path remains active. Git history is the rollback record.

If upstream covers only part of the plugin contract, keep the plugin only for the remaining gap, narrow it where practical, and document the residual behavior. Fork synchronization does not itself mutate an active runtime: when retirement requires profile, canonical-source, or runtime changes outside this repository, report the exact retirement work and treat native replacement as incomplete until the separate promotion removes the plugin and verifies the live result.

## Maintained patch index

| ID | Status | Stable commit subject | Purpose |
| --- | --- | --- | --- |
| HERMES-001 | Retired | `chore(local): carry Brian-owned working-tree patches into the fork`; `docs(fork): retire state repair patch` | Historical malformed `state.db` repair serialization, replaced by released upstream commit `923d86e09`. |
| HERMES-002 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Make raw SQLite backup and quarantine connection-safe. |
| HERMES-003 | Retired | `chore(local): carry Brian-owned working-tree patches into the fork`; `docs(fork): retire fd soft-limit patch` | Historical fixed 8192 file-descriptor floor, replaced by upstream's configurable runtime limit. |
| HERMES-004 | Active | `chore(local): carry Brian-owned working-tree patches into the fork`; `fix(telegram): atomically reserve per-chat sends`; `fix(telegram): preserve bounded cooldown semantics` | Enforce a per-chat Telegram send cooldown. |
| HERMES-005 | Active | `chore(local): carry Brian-owned working-tree patches into the fork`; `fix(fork): preserve reconciled patch contracts` | Share the progress-edit throttle per chat. |
| HERMES-006 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Resolve memory notifications per platform. |
| HERMES-007 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Keep interrupt sentinels out of API assistant text. |
| HERMES-008 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Preserve Hindsight's explicit shared observation scope. |
| HERMES-009 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Fail Hindsight retains on extraction errors. |
| HERMES-010 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Avoid destructive Hindsight daemon restarts and empty-key overwrite. |
| HERMES-011 | Active | `fix(state): serialize public reads, bound readers, one gateway SessionDB`; `test(state): align shared SessionDB ownership regressions` | SessionDB concurrency, reader lifecycle, and gateway ownership. |
| HERMES-012 | Retired | `chore: automate maintained fork synchronization`; `fix: use fork-safe candidate verification`; `fix: promote only dispatched fork candidates`; `chore(fork): enforce maintained patch manifest`; `chore(fork): adopt root maintenance manifest`; `chore(fork): retire GitHub sync workflows`; `docs(fork): retire plugins superseded upstream`; `fix(sync): reconcile fork patches with current upstream APIs`; `fix(fork): remove duplicate reconciled toolset entry`; `docs(fork): reconcile maintenance ownership and patch registry` | Historical GitHub Actions synchronization pipeline, replaced by unified `maintain-targets` ownership and dedicated repository reconciliation. |
| HERMES-013 | Active | `feat(cron): support per-job timezones` | Explicit IANA timezone pins for individual cron jobs. |
| HERMES-014 | Active | `fix(cron): propagate CLI failures` | Return cron subcommand failure status through the top-level CLI dispatcher. |
| HERMES-015 | Active | `fix(cwd): isolate gateway sessions from cron workdirs` | Keep a workdir cron's process-global cwd override out of concurrent gateway prompts and tools. |
| HERMES-016 | Active | `fix(config): preserve flat MoA settings during merge`; `docs(maintenance): track flat MoA merge patch` | Prevent inherited default presets from shadowing explicit flat MoA configuration. |
| HERMES-017 | Active | `feat(title): add compact titles and canonical aliases`; `feat(titles): configure concise distinct session titles`; `feat(titles): support configurable casing`; `fix(titles): reject malformed auxiliary output`; `fix(titles): reject incomplete markdown fences`; `fix(titles): harden Gemini fallback generation`; `docs(maintenance): track related title fixes`; `fix(titles): reuse healthy auxiliary routes`; `fix(auxiliary): preserve route attribution with complete responses`; `test(auxiliary): stabilize timeout deadline` | Make title shape configurable while preserving durable, race-safe uniqueness and the usable derived title when model output is malformed or truncated. |
| HERMES-018 | Active | `feat(telegram): add semantic topic icons and robust auto-renames`; `feat(telegram): remember 24 recent topic icons`; `fix(fork): preserve reconciled patch contracts`; `fix(telegram): fall back deterministically for topic icons`; `fix(titles): keep completed responses in primary provider`; `feat(telegram): title topics after completed replies`; `fix(telegram): suppress duplicate topic title updates`; `fix(telegram): defer topic metadata until response`; `fix(telegram): suppress derived topic rename` | Select live Telegram topic icons without repeating the 24 most recent choices, overwriting manual icons, or silently leaving an eligible topic iconless after model-selection failure. |
| HERMES-019 | Active | `fix(slack): ignore hidden parent metadata updates`; `test(slack): prove parent replay isolation` | Prevent Slack reply bookkeeping from replaying an old thread parent as a fresh user turn after a gateway restart. |
| HERMES-020 | Active | `fix(skills): limit background review creation`; `fix(config): recognize background skill creation policy`; `test(skills): align ledger with background creation policy` | Allow background review updates while disabling autonomous creation of new skills through configuration. |
| HERMES-021 | Active | `fix(agent): try alternate credential before provider fallback`; `fix(agent): soften transient credential overloads`; `fix(agent): clear stale rotation telemetry`; `fix(agent): generalize transient alternate credential recovery` | Try one alternate compatible same-provider credential for recoverable upstream failures before activating the fallback model. |
| HERMES-022 | Active | `feat(cron): job-scoped native outbound messages`; `fix(fork): preserve reconciled patch contracts`; `fix(cron): reconcile outbound retry states`; `docs: record upstream PR feedback state`; `fix(cron): keep outbound adapter profile-bound`; `fix(cron): preserve outbound run identity across retries`; `fix(cron): recover pre-send outbound claims`; `fix(cron): preserve ambiguous post-send errors`; `fix(cron): bind native sends to owning profile`; `fix(review): restore backup and cron ownership fences`; `fix(review): fence cron ownership and profile-bound sends`; `fix(review): narrow origin normalization and fail closed on profile`; `fix(review): commit complete malformed backup bundles`; `fix(review): revoke stale cron outbound authority`; `fix(review): atomically fence cron transport and backup reuse`; `fix(review): fence outbound claims and validate source manifests`; `fix(review): stabilize backup publication and profile fallback`; `fix(review): validate final bundles and route trusted profiles`; `fix(review): recover interrupted manual cron executions`; `test(cron): bind trusted owner in outbound fixtures`; `fix(review): restore upstream cron and title contracts`; `fix(cron): keep verified outbound results immutable` | Restore opt-in cron `send_message` for one job at a time, with origin-only targeting, profile-bound adapter identity, and idempotent multi-message delivery. |
| HERMES-023 | Active | `fix(auxiliary): route provider overload through fallback chain` | Treat classified provider overload as auxiliary capacity failure in sync and async calls. |
| HERMES-024 | Active | `fix(auxiliary): retry transient failure on one alternate credential`; `fix(auxiliary): require exact failed credential identity`; `fix(auxiliary): bind and preserve alternate credential recovery` | Try one isolated same-provider pool credential before auxiliary model/provider fallback. |
| HERMES-025 | Active | `fix(compression): report aborted compaction accurately`; `test(compression): cover quiet terminal failure notice`; `fix(compression): collapse growth rejection notices` | Emit exactly one committed, aborted, or deferred terminal outcome instead of unconditional success or duplicate failure notices. |
| HERMES-026 | Active | `feat(memory): retain source material automatically`; `fix(memory): gate raw attachment retention`; `fix(memory): gate generic file extraction retention`; `fix(memory): constrain retained attachment paths`; `fix(memory): revalidate attachment bytes before upload` | Preserve source evidence while requiring explicit opt-in before retaining raw attachments or generic file reads. |
| HERMES-027 | Active | `fix: harden gateway runtime boundaries`; `fix(gateway): recover unacknowledged terminal responses` | Resume recent sessions after an unexpected exit unless outbound delivery is durably acknowledged. |
| HERMES-028 | Active | `fix: harden gateway runtime boundaries`; `fix(terminal): avoid remote Python lifecycle dependency`; `fix(terminal): skip known remote shell binaries`; `fix(terminal): trust only system shell paths` | Inspect referenced remote scripts through the POSIX shell contract without requiring Python. |
| HERMES-029 | Active | `fix(media): honor provider retry delays for downloads` | Make idempotent image/audio URL-cache GETs honor bounded provider retry timing. |
| HERMES-030 | Retired | `fix(launchd): preserve supervisor marker through gateway wrapper`; `docs(fork): register HERMES-030 launchd supervisor marker`; `fix(fork): retire launchd supervisor marker patch` | Historical generated-plist supervisor marker, replaced by upstream wrapper propagation in released commit `c69a0872ea`. |
| HERMES-031 | Active | `fix(output): preserve answers before verification receipts` | Keep a substantive answer when a verify-on-stop continuation returns only a verification receipt. |
| HERMES-033 | Active | `fix(compression): report LCM safe deferrals`; `fix(compression): classify LCM no-op as deferred`; `fix(compression): classify anti-growth rejection as deferred`; `fix(compression): defer automatic retries after growth rejection`; `fix(compression): preserve context engine compatibility` | Persistently defer automatic compression after a `would_grow` rejection while preserving manual retries and pluggable context-engine compatibility. |
| HERMES-032 | Active | `fix(doctor): make state db advisory retention aware` | Make large-state diagnostics distinguish configured retention from actionable retention or FTS problems. |
| HERMES-034 | Active | `feat(telegram): configure rich message routing mode` | Add explicit adaptive, always-attempt, and legacy-only Telegram Rich Message routing modes. |
| HERMES-035 | Active | `fix(telegram): remove excessive paragraph spacing` | Preserve visible Telegram paragraph separation without over-spacing lists, code, or native tables. |
| HERMES-036 | Active | `feat(cron): allow local memory opt-in`; `test(cron): verify local USER memory writes` | Let individual cron jobs opt into the local file-backed memory toolset without activating external memory providers. |
| HERMES-037 | Active | `fix(clarify): explain decisions before prompts`; `fix(gateway): preserve clarify decision context` | Keep decision context in normal assistant prose before interactive clarify prompts. |
| HERMES-038 | Active | `feat(hindsight): use brain indicator glyph` | Use the brain indicator only when Hindsight supplied retrieved context. |
| HERMES-039 | Active | `feat(memory): clarify semantic and transcript recall guidance`; `feat(memory): prefer observations and curate hindsight memories`; `feat(memory): expand explicit recall context by default`; `fix(context): keep retrieved provider context request-scoped`; `refactor(prompt): scope retrieval guidance by capability`; `test(memory): align request-scoped gateway turn context` | Keep retrieved memory request-scoped while giving capability-aware semantic and transcript recall guidance. |
| HERMES-040 | Active | `fix(gateway): extract local markdown images` | Extract eligible local Markdown images at the gateway media boundary. |
| HERMES-041 | Active | `fix(gateway): fail closed on inherited restart marker`; `fix(safety): block destructive gateway launchctl verbs` | Prevent gateway-derived contexts from bypassing lifecycle self-control guards. |
| HERMES-042 | Active | `fix(memory): pin hindsight client to 0.9.1` | Pin the Hindsight client and align the bundled provider with its supported 0.9.1 contract. |
| HERMES-043 | Active | `fix(gateway): skip completed legacy resumes` | Avoid re-running completed legacy sessions during startup continuation recovery. |
| HERMES-044 | Active | `fix(output): compose and protect final responses` | Compose plugin output transforms without letting empty or non-substantive transforms erase the final answer. |

## Fork-only administrative subject exemptions

These exact subjects are fork-only history but do not define independently retireable product behavior. The validator requires every other fork-only subject to appear in a patch-index row.

| Stable commit subject | Narrow non-patch reason |
| --- | --- |
| `fix(fork): restore upstream gateway contracts after rebase` | Patch-neutral restoration of upstream-owned gateway and finalization hunks clobbered by fork replay. |
| `fmt(js): npm run fix after upstream refresh` | Mechanical formatter output created during an upstream reconciliation. |
| `test: make local CI deterministic across hosts` | Repository test-runner determinism only; no shipped Hermes behavior. |
| `test: force sounddevice path in beep unit test` | Host-specific test-fixture repair only; no shipped Hermes behavior. |

The umbrella commit contains independently retireable fixes. Never revert it wholesale to retire one of HERMES-001 through HERMES-010.

## Patch records

### HERMES-033 — Defer automatic retries after growth rejection

- **Compatibility correction hypothesis (2026-08-17):** The `would_grow` commit guard added by HERMES-033 calls `record_rejected_compaction()` directly on the selected context engine, but that method exists only on the built-in `ContextCompressor` and is absent from the documented `ContextEngine` contract. A conforming external engine such as hermes-lcm therefore preserves the transcript but raises `AttributeError` before Hermes can return the deferred outcome. The correction belongs in the host-owned context-engine interface: define a backward-compatible no-op rejection-notification hook on `ContextEngine`, retain the built-in compressor's durable override, and prove the full anti-growth path with an alternative engine that does not override the hook. A call-site `getattr` guard would suppress the crash but leave the host contract implicit; adding the method to LCM alone would couple one plugin to a fork-private host detail and leave every other engine exposed. No state migration is required. Rollback removes the default hook and its compatibility regression while retaining the built-in HERMES-033 behavior.
- **Summary:** When core in-place compression generates a candidate whose rough token estimate is larger than the original transcript, the commit-site anti-growth guard preserves the original transcript and returns a deferred outcome. This patch records that `would_grow` result as a strong ineffective automatic-compaction verdict for the built-in compressor, persists its existing anti-thrashing breaker, and blocks the next automatic retry while keeping manual `/compress` available. Alternative context engines inherit a no-op rejection-notification hook, so the host preserves their transcript and returns the same deferred outcome without requiring plugin-specific anti-thrashing state. No committed boundary or post-compaction real-usage verification is recorded.
- **Surfaces:** `agent/context_engine.py`; `agent/context_compressor.py`; `agent/conversation_compression.py`; `tests/agent/test_compression_anti_thrash_persistence.py`; `tests/run_agent/test_413_compression.py`.
- **Upstream tracking:** Issue #88568. Direct open PR #88593 implements the strike recording and currently has green CI but no reviews; its broad call-site exception handler avoids crashing alternative engines but can also hide built-in persistence failures, and it does not define the rejection hook on the context-engine contract. Related safety fix: #86700. Related gateway cooldown work: #79540 and #79876. Related small-window inflation work: #23811, #21470, and #25413. No equivalent released upstream behavior was identified through upstream `9b112e5736` on 2026-08-17.
- **Upstream PR:** Direct: #88593 (open, unreviewed, green CI; checked 2026-08-17). Its compatibility behavior is narrower than this patch contract.
- **Regression:** `pytest -q tests/run_agent/test_413_compression.py::TestPreflightCompression::test_compress_context_emits_deferred_terminal_status_for_would_grow tests/run_agent/test_413_compression.py::TestPreflightCompression::test_would_grow_remains_deferred_for_alternative_context_engine tests/agent/test_compression_anti_thrash_persistence.py::TestStrikesPersistFromEveryVerdictSite tests/agent/test_compression_anti_thrash_recovery.py`.
- **Rollback:** Remove the default `ContextEngine.record_rejected_compaction` hook and its alternative-engine regression to roll back only the compatibility correction. To retire all of HERMES-033, additionally remove the built-in `ContextCompressor.record_rejected_compaction` override, its `would_grow` call site, and the focused persistence cases. Preserve the existing anti-growth transcript-preservation guard and all other anti-thrashing verdict paths.
- **Retirement:** Retire after released upstream records an equivalent core `would_grow` rejection as a durable automatic-compaction deferral, preserves manual retries, preserves alternative context-engine compatibility without hiding built-in persistence failures, and passes the focused persistence, plugin-engine, and restart regressions.

### HERMES-032 — Make large-state diagnostics retention-aware

- **Independent hypothesis (2026-08-16):** Doctor derives its large-`state.db` issue by searching rendered advisory text for `auto_prune`, without loading the effective session-retention configuration. That makes database size itself actionable even when positive retention is already configured. The correction belongs in Doctor's diagnostic boundary: resolve effective configuration through `hermes_cli.config.load_config()`, carry explicit issue metadata separately from rendered detail, and keep FTS storage warnings independent.
- **Summary:** Treats an oversized `state.db` as informational when `sessions.auto_prune` is enabled with a valid positive integer `retention_days`, including a numeric string. Disabled, invalid, or unavailable retention remains actionable. Pending or legacy FTS storage continues to recommend offline `hermes sessions optimize-storage`. Messaging states that pruning removes ended inactive sessions, does not cap active-session growth, and does not itself shrink the SQLite file.
- **Surfaces:** `hermes_cli/doctor.py`; `tests/test_state_db_stats.py`.
- **Upstream tracking:** Issue #83933 remains open and directly reports the false `auto_prune` recommendation. Open PR #83954 is a narrower associated fix. Closed-unmerged PR #84091 proposed a related retention-aware severity model. Open PR #86271 is broader health-diagnostic work and is not an equivalent replacement. No released upstream implementation touched the affected files as of 2026-08-17 at upstream `93ed11379b`.
- **Upstream PR:** Associated: #83954 (open, unmerged, no review decision; checked 2026-08-17). Related: #84091 (closed unmerged) and #86271 (open; checked 2026-08-17).
- **Regression:** `uv run pytest tests/test_state_db_stats.py tests/hermes_cli/test_doctor.py tests/hermes_cli/test_doctor_journal_modes.py -q`; `uv run ruff check hermes_cli/doctor.py tests/test_state_db_stats.py`; `git diff --check`; and a real `uv run hermes doctor` against an oversized retained database.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated Doctor changes. Remove `_session_retention_policy`, the explicit advisory issue field, retention-aware severity, and only HERMES-032's focused tests. Restore the prior tuple contract and issue construction without changing state-size, WAL, FTS, pruning, VACUUM, or runtime-maintenance behavior.
- **Retirement:** Retire after released upstream loads effective retention configuration, treats a large database with valid positive retention as informational, keeps invalid or unavailable retention and pending or legacy FTS actionable, avoids rendered-text issue inference, and passes the focused regressions plus a real Doctor canary.

### HERMES-031 — Preserve substantive answers before verification receipts

- **Independent hypothesis (2026-08-16):** The verify-on-stop continuation correctly preserves the attempted answer when its budget is exhausted, but a later model response consisting only of a verification receipt can still become `final_response` and replace the substantive answer. The correction belongs at the finalization boundary: recognize only receipt-prefixed continuation output, compose it after the pending answer, and leave complete later answers authoritative.
- **Summary:** Prevents a verify-on-stop or `pre_verify` continuation from replacing a substantive answer with a receipt-only response such as `Fresh verification from this turn passes`. The answer remains first and the receipt is appended under `## Verification`.
- **Surfaces:** `agent/turn_finalizer.py`; `tests/run_agent/test_verification_continuation_budget.py`.
- **Upstream tracking:** Issue #53828 remains open and directly describes this contract. Related merged messaging mitigation is PR #52412, but it does not cover an explicit `agent.verify_on_stop: true` override. Related response-loss work includes issue #62142 and closed PR #53553.
- **Upstream PR:** None after checked 2026-08-16.
- **Regression:** `pytest -q tests/run_agent/test_verification_continuation_budget.py`.
- **Rollback:** Remove `_VERIFICATION_RECEIPT_PREFIX`, `_compose_verification_receipt_with_answer`, its finalization call, and the two focused tests. Preserve the existing budget-exhaustion fallback and ordinary later-answer replacement behavior.
- **Retirement:** Retire after released upstream preserves a pending substantive answer whenever a verification continuation returns only a receipt, proven by the focused regression and a real Telegram or equivalent messaging-surface canary.

### HERMES-030 — Preserve the launchd supervisor marker across the stderr wrapper

- **Retired (2026-08-18):** Released upstream commit `c69a0872ea` now preserves supervision at the actual wrapper boundary: `stderr_timestamp` detects a real nonzero launchd `XPC_SERVICE_NAME` on itself and passes `HERMES_GATEWAY_EXTERNAL_SUPERVISOR=1` only to its gateway child. Interactive `XPC_SERVICE_NAME=0` children remain unmarked. The private generated-plist environment entry and its duplicate tests were removed; upstream's wrapper tests now own the contract. Runtime regeneration and a live launchd canary remain part of a separately authorized promotion and were not performed during source reconciliation.
- **Observed failure (2026-08-15):** Promoting the fork sync (`0.20.0` → `0.20.1`, runtime repo at `401d808610`) ran `hermes gateway start`, which regenerated `ai.hermes.gateway.plist`. Upstream `1db9273584 fix(gateway): timestamp launchd error log lines` had changed `ProgramArguments` from a direct `python -m hermes_cli.main gateway run --replace` exec to `python -m hermes_cli.stderr_timestamp --error-log … -- python -m hermes_cli.main gateway run --replace`. From that rewrite the runtime gateway never started again: every spawn printed `A gateway is already running under launchd for this profile.` and exited 1, and `KeepAlive` respawned it every `ThrottleInterval` (30s) indefinitely, so Hermes was fully down until the marker was restored. Measured on the live job: the gateway process reported `XPC_SERVICE_NAME="0"` with its ppid being the wrapper.
- **Root cause:** launchd stamps `XPC_SERVICE_NAME` with the job label only onto the process it spawns **directly**. With the wrapper in `ProgramArguments` that process is `stderr_timestamp`, and macOS resets the `Popen`-ed grandchild's `XPC_SERVICE_NAME` to the sentinel `"0"` rather than inheriting the label (macOS actively manages this variable — a process falsely claiming a service name is `SIGABRT`ed). `is_gateway_supervisor_process()` treats `"0"` as "an interactive shell launched me", so `_guard_supervised_gateway_conflict()` sees `service_installed=True, service_running=True` from launchd's own registration, concludes a *different* gateway owns the profile, and `sys.exit(1)`s on the service's own startup — the exact respawn/refuse loop its docstring warns against.
- **Summary:** The historical private implementation declared `HERMES_GATEWAY_EXTERNAL_SUPERVISOR=1` in generated launchd plists. Released upstream now owns the same safety contract more narrowly in `hermes_cli/stderr_timestamp.py`, forwarding the marker only when the wrapper itself has a real launchd service label.
- **Surfaces:** Historical private surfaces were `hermes_cli/gateway.py` and two cases in `tests/hermes_cli/test_gateway_service.py`. The active replacement is upstream `hermes_cli/stderr_timestamp.py` with `tests/hermes_cli/test_stderr_timestamp.py`.
- **Upstream tracking:** Replaced by released upstream commit `c69a0872ea` (contained in tags `v2026.8.16`, `v2026.8.16.2`, and `v2026.8.18`), which closed issue #86893 on 2026-08-16 after four independent macOS reproductions.
- **Upstream PR:** None; the released replacement is tracked as commit `c69a0872ea` and issue #86893 (checked 2026-08-18).
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_stderr_timestamp.py tests/hermes_cli/test_gateway_service.py -q`. Runtime promotion separately regenerates the plist and proves one live launchd start without a respawn loop.
- **Rollback:** Do not restore the private generated-plist marker or duplicate tests. If wrapper propagation regresses, repair or backport upstream's `_child_env_for_command()` boundary while preserving the rule that interactive `XPC_SERVICE_NAME=0` processes are never marked supervised.

### HERMES-029 — Provider-directed retries for idempotent media downloads

- **Independent hypothesis (2026-08-15):** `cache_image_from_url` and `cache_audio_from_url` currently retry every HTTP status at or above 429 and every `httpx.TimeoutException` after fixed 1.5s/3s delays. A 429/503 carrying `Retry-After` can therefore be retried before the provider permits, while permanent 5xx statuses and ambiguous read timeouts are retried unnecessarily. The correction belongs in one shared internal URL-cache GET helper: retry only 429/502/503/504 plus connection establishment failures, parse both Retry-After delta-seconds and HTTP-date through the existing shared parser, choose a bounded exponential delay with jitter that is never earlier than the provider deadline, and fail closed when retries or the cumulative wait budget are exhausted. Image/audio validation, SSRF redirect checks, byte caps, and outbound send behavior remain unchanged.
- **Summary:** Shares bounded retry policy across idempotent image and audio URL-cache GETs. Provider Retry-After seconds or HTTP-date is a minimum delay; excessive delays fail/defer instead of sleeping past the media-download budget. Only 429/502/503/504, connect errors, and connect timeouts retry. Permanent HTTP failures and post-connect/read failures fail closed. Retry logs sanitize signed URLs.
- **Surfaces:** `gateway/platforms/base.py`; `tests/gateway/test_media_download_retry.py`; `tests/gateway/test_media_download_retry_after.py`.
- **Upstream tracking:** No equivalent implementation was found on current official `main` (`30c469b15313711d47c45e7175d6ef5c8437f1ed`) after source/history and issue/PR/commit searches for the two cache helper symbols plus Retry-After on 2026-08-15. Upstream already supplies the shared seconds/HTTP-date parser in `agent.retry_utils`, which this patch reuses.
- **Upstream PR:** None after checked 2026-08-15.
- **Regression:** `.venv/bin/python -m pytest tests/gateway/test_media_download_retry_after.py tests/gateway/test_media_download_retry.py tests/gateway/test_platform_base.py -q`.
- **Rollback:** Remove `_download_media_from_url` and its media retry constants/helpers, restore the separate image/audio request loops and prior timeout fixture, and remove `tests/gateway/test_media_download_retry_after.py`. Preserve SSRF validation, redirect hooks, streaming byte limits, cache validation, and all outbound platform retry semantics.
- **Retirement:** Retire after released upstream shares equivalent image/audio URL-cache GET retry behavior that honors delta/date Retry-After as a minimum, bounds cumulative waiting, retries only the same safe statuses/transports, sanitizes logs, and passes the focused regression.


### HERMES-028 — Inspect remote scripts without a Python dependency

- **Summary:** Keeps the gateway self-control guard fail-closed while reading bounded referenced scripts through the target's POSIX `sh` and `dd` contract. Supported SSH, container, and sandbox targets no longer need Python merely to run an otherwise valid shell script. Canonical absolute shell paths under immutable system directories (`/bin`, `/usr/bin`) are treated as interpreters, while their script arguments remain recursively inspected; user-writable `/usr/local` and Homebrew shells are inspected as executable content.
- **Surfaces:** `cron/lifecycle_guard.py`; `tools/terminal_tool.py`; `tests/hermes_cli/test_gateway_restart_loop.py`.
- **Upstream tracking:** Local fork correction to the concurrent gateway-boundary hardening; no released upstream equivalent was identified.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/hermes_cli/test_gateway_restart_loop.py -k 'remote_backend or referenced_script'`.
- **Rollback:** Replace the remote reader only with another bounded target-environment primitive that is guaranteed by every supported backend. Preserve fail-closed behavior for unreadable referenced scripts and the gateway self-control block.
- **Retirement:** Retire after released upstream performs equivalent bounded remote script inspection without adding runtime dependencies beyond the supported remote shell contract.

### HERMES-027 — Recover recent sessions until delivery is acknowledged

- **Summary:** Treats a persisted terminal assistant transcript as model-completion evidence, not outbound-delivery evidence. After an unexpected gateway exit, recent non-suspended sessions remain eligible for recovery because a crash can occur after transcript persistence but before the platform adapter confirms delivery.
- **Surfaces:** `gateway/session.py`; `tests/gateway/test_clean_shutdown_marker.py`.
- **Upstream tracking:** Local fork correction to the concurrent gateway-boundary hardening; no released upstream durable delivery acknowledgement was identified.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/gateway/test_clean_shutdown_marker.py`.
- **Rollback:** Restore terminal-transcript suppression only after the platform delivery path writes a durable acknowledgement that is atomically associated with the transcript turn. Never infer delivery from `finish_reason=stop` alone.
- **Retirement:** Retire after released upstream recovery suppresses only turns with durable confirmed delivery and preserves generated-but-undelivered responses across gateway crashes.

### HERMES-026 — Retain source evidence without implicit raw attachment upload

- **Summary:** Automatically retains substantive source-like pasted text, complete supported tool extractions, and durable text artifacts with stable provenance and readback verification. Raw file attachments and generic `read_file` output cross stronger trust boundaries. Discovery accepts attachment bytes only from canonical profile-aware media cache roots and only when `retain_attachments: true`; the asynchronous upload boundary reopens with no-follow semantics, revalidates the root and regular-file type, enforces a 25 MiB bound, and verifies the original content hash. It does not retain generic file extraction text unless `retain_file_extractions: true`. Both booleans default to false.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; `plugins/memory/hindsight/source_retention.py`; `tests/plugins/memory/test_source_retention.py`; `tests/plugins/memory/test_source_retention_canary.py`.
- **Upstream tracking:** Local fork behavior; no released upstream equivalent or privacy-gated source-retention contract has been identified.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/plugins/memory/test_source_retention.py tests/plugins/memory/test_source_retention_canary.py`.
- **Rollback:** Remove automatic source discovery, source-ledger/readback tracking, and HERMES-026 tests together. Preserve ordinary conversation retain, HERMES-008 observation scopes, HERMES-009 extraction-error handling, and HERMES-010 embedded-daemon safety.
- **Retirement:** Retire after released upstream preserves equivalent source provenance and durability while keeping raw attachment reads/uploads default-off behind an explicit trust-boundary opt-in.

### HERMES-025 — Outcome-aware compaction lifecycle

- **Growth-rejection correction hypothesis (2026-08-17):** The anti-growth path added after HERMES-025 emits a detailed `warn` event directly and then completes the already-started lifecycle with a second generic `compaction_deferred` event. Telegram correctly renders both visible events, so one rejected candidate becomes two near-duplicate user messages even though the lifecycle contract requires one truthful terminal edge. The correction belongs at the host compaction outcome boundary: preserve the warning-level log and telemetry, remove the direct user-facing warning, and pass the detailed growth explanation as the single `compaction_deferred` terminal message. Filtering one string in Telegram would leave duplicate semantic events on other surfaces and couple transport policy to a core lifecycle bug. No state, schema, provider, or plugin behavior changes. A focused regression must fail by observing `lifecycle, warn, compaction_deferred` before the fix and pass with only `lifecycle, compaction_deferred` afterward.
- **Summary:** Replaces the unconditional post-compression `compacted` event with one truthful terminal edge: `compacted` after a committed transcript boundary, `compaction_aborted` after failed work that preserves the prior transcript, or `compaction_deferred` when another path or a cooldown prevents the attempt. Summary, empty-transcript, Codex-native, and anti-growth failures carry their detailed failure text in that single terminal event rather than emitting a failure followed by false success or a duplicate generic deferral.
- **Surfaces:** `agent/conversation_compression.py`; `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts`; `apps/desktop/src/app/session/hooks/use-message-stream/compaction-event.test.tsx`; `tests/run_agent/test_413_compression.py`; `tests/run_agent/test_codex_app_server_compaction.py`; `tests/gateway/test_telegram_noise_filter.py`.
- **Upstream tracking:** No exact issue or PR for duplicate anti-growth notices was found after current open/closed issue and PR searches on 2026-08-17 against upstream `bc76f62c20`. Related issue #88568 and direct open PR #88593 cover repeated automatic `would_grow` attempts, not the duplicate lifecycle delivery. Related released anti-growth safety work is #86700.
- **Upstream PR:** None for the duplicate-notice correction after checked 2026-08-17. Related: #88593 (open, mergeable, unreviewed) for anti-thrashing only.
- **Regression:** `pytest -q tests/run_agent/test_413_compression.py::TestPreflightCompression::test_compress_context_emits_deferred_terminal_status_for_would_grow`; then `pytest -q tests/run_agent/test_413_compression.py tests/run_agent/test_codex_app_server_compaction.py tests/gateway/test_telegram_noise_filter.py`.
- **Rollback:** Remove the detailed `would_grow` terminal-message override and restore the separate direct warning only if another released mechanism deduplicates semantic lifecycle events on every surface. Preserve the anti-growth guard, rejection recording, telemetry, transcript preservation, and exactly-one terminal lifecycle contract. Full HERMES-025 rollback restores `_emit_compaction_done` and removes the outcome/message state and HERMES-025 tests, while preserving all compression locking, commit-fence, cooldown, and transcript-preservation behavior.
- **Retirement:** Retire after released upstream emits exactly one equivalent terminal outcome per visible compaction lifecycle, including a detailed single notice for `would_grow`, and never reports completion or a second warning for an aborted or deferred attempt.

### HERMES-024 — Retry auxiliary transient failure on one alternate credential

- **Summary:** For classified provider overload, server-error, and timeout failures, sync and async auxiliary calls select one distinct healthy runtime credential from the matching provider pool only when the failed runtime credential is exactly attributable. The retry binds that credential to an isolated auxiliary client, leaves the main conversation route and pool cursor unchanged, applies only a short soft cooldown to the failed entry, and never marks either credential exhausted for a transient failure.
- **Surfaces:** `agent/auxiliary_client.py`; `tests/agent/test_auxiliary_client.py`.
- **Upstream tracking:** Local fork behavior; upstream equivalence has not yet been established.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/agent/test_auxiliary_client.py -k 'AuxiliaryTransientCredentialRetry or AuxiliaryOverloadFallback'`.
- **Rollback:** Remove `_transient_credential_retry_reason`, `_select_transient_aux_alternate`, the sync/async alternate retry blocks, and the HERMES-024 focused tests. Preserve HERMES-023 overload fallback and all durable auth, billing, and rate-limit rotation behavior.
- **Retirement:** Retire after released upstream provides provider-neutral, non-exhausting, bounded same-provider credential alternation for equivalent auxiliary transient failures before model/provider fallback.

### HERMES-023 — Route auxiliary provider overload through fallback

- **Summary:** Uses the shared API error classifier to recognize provider-overload responses, including status-less overload messages, as auxiliary capacity failures. Sync and async auxiliary calls now continue through the configured model/provider fallback chain after overload instead of aborting compression or another side task.
- **Surfaces:** `agent/auxiliary_client.py`; `tests/agent/test_auxiliary_client.py`.
- **Upstream tracking:** Local fork behavior; upstream equivalence has not yet been established.
- **Upstream PR:** None.
- **Regression:** `pytest -q tests/agent/test_auxiliary_client.py -k 'AuxiliaryOverloadFallback'`.
- **Rollback:** Remove `_is_overload_error`, its sync/async fallback predicates and reason labels, and the HERMES-023 focused tests. Preserve all existing auth, billing, connection, rate-limit, model-compatibility, and response-validation fallback behavior.
- **Retirement:** Retire after released upstream routes classified provider overload through equivalent sync and async auxiliary fallback chains.

### HERMES-022 — Job-scoped native outbound messages for cron

- **Summary:** Restores `send_message` as a default-off `messaging` toolset and lets a single cron job opt in with `allow_messaging=true`. The opted-in job may send multiple native messages through the configured Hermes adapter identity, but only to the job's bound origin. Each claimed firing receives a durable run identity that survives stale-claim recovery. Pre-send `queued` records are reclaimable; an atomic transport-start fence becomes `ambiguous` before adapter invocation, preventing duplicate retries after an uncertain send. Adapter errors remain ambiguous unless explicit `delivery_stage="pre_send"` evidence proves transport never began. `[SILENT]` suppresses only the scheduler's final automatic delivery, and the tool cannot select another account, profile, or chat ID.
- **Surfaces:** `cron/jobs.py`; `cron/scheduler.py`; `cron/outbound.py`; `tools/send_message_tool.py`; `tools/cronjob_tools.py`; `toolsets.py`; `tests/cron/test_cron_outbound_messages.py`; `tests/cron/test_scheduler.py`; `tests/cron/test_jobs.py`; `tests/cron/test_cronjob_schema.py`.
- **Upstream tracking:** Direct PR `#86648` implements this patch contract on current upstream `main` and links issues `#20140` and `#67591`. Its 2026-08-16 automated review raised three items. The edit-preservation concern was valid as a missing regression: the CLI already forwards `None` without coercion and the update boundary already treats it as “unchanged,” now proven by an unrelated timezone edit preserving `allow_messaging=true`. The result-downgrade concern was valid and fixed in the fork by making a durable `verified` record immutable against late callbacks. The toolset-name concern is stale against this fork: `messaging` is an explicit default-off capability, no platform base toolset includes it, and runtime send gates remain a second boundary. Related open PRs `#7388` and `#70304` remain incomplete: `#7388` is a process-wide env var, `#70304` restores a broader platform-level messaging toolset, and neither provides job-scoped origin-only targeting plus idempotent multi-message delivery. Removal context: merged PR `#47856`.
- **Upstream PR:** Direct: #86648 (open, no reviews or review threads, one automated issue comment addressed as classified above, head checks not reported; checked 2026-08-18). Related: #7388 (open, failing head checks, one unresolved review thread) and #70304 (open, failing head checks, no unresolved review threads). Removal context: #47856 (merged).
- **Regression:** `pytest -q tests/cron/test_cron_outbound_messages.py tests/cron/test_scheduler.py tests/cron/test_jobs.py tests/cron/test_cronjob_schema.py -k 'disabled_toolsets or memory_toolset or PerJobToolset or allow_messaging or fire_claim_run_id'`.
- **Rollback:** Remove the `allow_messaging` field, the cron outbound ledger, the cron-only `send_message` registration/check, the origin-only send gate, the `[SILENT]` exception for explicit outbound messages, and the HERMES-022 tests. Restore the previous default cron denylist and delivery hint. Do not restore a process-wide messaging env var.
- **Retirement:** Retire after released upstream provides job-scoped opt-in, origin-only targeting, adapter-owned identity, idempotent multi-message delivery, and `[SILENT]` that does not suppress those explicit messages.

### HERMES-020 — Limit background review skill creation

- **Summary:** Adds `skills.background_review_allow_create`, enforced at the `skill_manage` runtime boundary for the `background_review` origin only. `false` blocks `create` while preserving foreground creation and background updates to existing skills.
- **Surfaces:** `tools/skill_manager_tool.py`; `cli-config.yaml.example`; `tests/tools/test_skill_manager_tool.py`.
- **Upstream tracking:** Local fork behavior; no released upstream setting currently provides this selective policy.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/tools/test_skill_manager_tool.py`.
- **Rollback:** Remove the create guard, its focused test, the example setting, and this manifest entry in one follow-up commit. Preserve the existing background ownership and read-before-write guards.

### HERMES-021 — Try alternate same-provider credentials before provider fallback

- **Summary:** Adds a bounded, provider-neutral alternate-credential attempt in the credential-pool recovery path. A timeout, server error, or provider overload may swap to one different healthy entry from the active provider's matching pool without marking the failing credential exhausted; existing rate-limit and billing paths continue to exhaust and rotate credentials, while authentication failures and upstream-aggregator 429s retain their existing refresh/fallback behavior. A 60-second process-local, profile-scoped soft cooldown steers nearby pool instances away from the transiently failing credential without making the pool unavailable, and structured rotation/outcome logs correlate the serving entry with latency and provider-reported prompt-cache usage while turn-boundary and interruption cleanup prevents stale outcome attribution. This prevents a transient provider-edge failure from jumping directly to a fallback model or immediately routing nearby calls back to the same overloaded credential while another compatible same-provider credential remains healthy.
- **Surfaces:** `agent/agent_runtime_helpers.py`; `agent/conversation_loop.py`; `agent/credential_pool.py`; `agent/turn_retry_state.py`; `run_agent.py`; `tests/agent/test_credential_pool_routing.py`; `tests/agent/test_turn_retry_state.py`; `tests/run_agent/test_run_agent.py`.
- **Upstream tracking:** Issue `#22916` asks for same-provider profile rotation before provider fallback but remains open without implementation. PRs `#24539` and `#11034` instead eagerly activate fallback for overloaded providers; PR `#84128` extends same-account Codex backoff. None provides one alternate same-provider account attempt before provider fallback.
- **Upstream PR:** None directly implements the patch after checked 2026-08-14. Related: #24539, #11034, and #84128.
- **Regression:** `scripts/run_tests.sh tests/agent/test_credential_pool_routing.py`.
- **Rollback:** Remove the alternate-entry selector, transient soft-cooldown registry, rotation/cache telemetry, and overload/transport branch from `recover_with_credential_pool`, then remove only the HERMES-021 focused tests. Preserve rate-limit retry semantics, billing rotation, auth refresh behavior, prompt-cache key construction, and upstream-aggregator fallback bypass.
- **Retirement:** Retire after a released upstream implementation provides capability-based same-provider credential alternation before provider fallback for transient overload, server-error, and timeout failures and passes equivalent focused tests.

### HERMES-001 — Retired malformed `state.db` repair serialization

- **Summary:** The private writable-schema repair lock, post-lock re-probe, schema-cookie bump, and duplicate fork tests have been removed. Released upstream commit `923d86e09` now owns the complete locking, re-probe, schema-cookie, hard-stop backup, and regression contract.
- **Surfaces:** Historical private surfaces were `hermes_state.py` and four patch-owned cases in `tests/test_state_db_malformed_repair.py`. The active replacement is upstream's repair implementation and tests in those same files.
- **Upstream tracking:** Replaced by released upstream commit `923d86e09`, the released descendant of the work previously tracked through PRs `#69609` and `#71982`.
- **Upstream PR:** Associated historical PRs: #69609 and #71982; released replacement commit: `923d86e09` (verified 2026-08-14).
- **Regression:** `scripts/run_tests.sh tests/test_state_db_malformed_repair.py` against the upstream-backed implementation.
- **Rollback:** Do not restore the private implementation or duplicate tests. If the upstream contract regresses, repair or backport upstream's coherent lock/re-probe/schema-cookie/hard-stop path; do not layer a second repair lock over it. HERMES-002's atomic raw-copy guard remains independent and must be preserved.

### HERMES-002 — Make raw SQLite backup and quarantine connection-safe

- **Summary:** Holds `offline_file_access` across the live-connection check and byte-level copy/fingerprint operation, closing the check/use race that could cancel POSIX SQLite locks.
- **Surfaces:** `hermes_state.py`; `hermes_cli/kanban_db.py`; `tests/test_raw_copy_offline_guard.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/test_raw_copy_offline_guard.py`.
- **Rollback:** Remove the `_copy_all`/`offline_file_access` guarded backup path in `hermes_state.py` and `_backup_corrupt_db_locked` guarded quarantine path in `hermes_cli/kanban_db.py`, then remove `tests/test_raw_copy_offline_guard.py`. Preserve upstream's released HERMES-001 replacement and all unrelated database-repair behavior. Verify the upstream replacement with the same live-connection race cases before deleting the private test.

### HERMES-003 — Retired fixed file-descriptor soft-limit floor

- **Summary:** The private pre-dispatch helper that best-effort raised `RLIMIT_NOFILE` to a fixed 8192 has been removed. Upstream now owns the complete contract through profile-aware `runtime.nofile_soft_limit`, a shared `apply_nofile_soft_limit()` helper for gateway and dashboard/serve entrypoints, and matching generated-service limits. The upstream implementation preserves the private safety properties: POSIX-only, best-effort, never lowers an existing limit, and clamps to a finite hard limit.
- **Surfaces:** Historical private surfaces were `hermes_cli/main.py` and `tests/test_fd_soft_limit.py`. The active replacement is upstream `hermes_cli/resource_limits.py`, its gateway/dashboard call sites, configuration, service generators, and upstream tests.
- **Upstream tracking:** Replaced by released upstream commits `87aedbe7b`, `0472c31aa`, `373631bea`, and `acb7547da` (including the configurable process and service-manager limit contract). Related historical issues were `#36899` and `#75269`.
- **Upstream PR:** None; replacement landed as released commits rather than a tracked PR in this record (checked 2026-08-14).
- **Regression:** Upstream resource-limit tests plus the repository's canonical suite. Runtime supervisor acceptance remains part of a separately authorized deployment, not fork synchronization.
- **Rollback:** Do not restore the private helper or test. If the upstream replacement regresses, fix or backport the upstream `runtime.nofile_soft_limit` path as one coherent contract; do not layer a second pre-dispatch limit implementation over it.

### HERMES-004 — Enforce a per-chat Telegram send cooldown

- **Summary:** Inside `TelegramAdapter`, atomically reserves a per-chat slot immediately before every persistent message-delivery Bot API call, including rich messages, every chunk and fallback attempt, control messages, and native media. Telegram `RetryAfter` deadlines advance the same shared clock; lock acquisition plus cooldown waiting share a bounded budget; control boundaries preserve retry metadata; positively identified pre-send connection/pool timeouts do not consume a slot; and idle chat state is pruned. Standalone CLI/cron sends and draft/edit/typing APIs are outside this process-local contract.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/test_telegram_send_cooldown.py`.
- **Upstream tracking:** Related upstream pull request `#66722` remains open and unmerged.
- **Upstream PR:** Related: #66722 (open; checked 2026-08-14).
- **Regression:** `pytest -q tests/test_telegram_send_cooldown.py`.
- **Rollback:** Remove `_TelegramSendCooldownExceeded`, the per-chat cooldown state maps and bound, `_send_cooldown_seconds`, `_send_cooldown_max_wait`, the atomic send helper and its call sites, then remove the dedicated test. Verify the upstream adapter atomically coordinates concurrent rich, chunked, fallback, control, and media calls per chat, shares `RetryAfter` deadlines, bounds excessive waits, and prunes idle state before deploying the removal.

### HERMES-005 — Share the progress-edit throttle per chat

- **Summary:** Coordinates progress edits across sessions sharing a chat, claims throttle slots before API calls, bounds clock storage, and avoids issuing a fresh send while Telegram is already flood-limiting edits.
- **Surfaces:** `gateway/run.py`; `tests/test_progress_edit_chat_throttle.py`; `tests/gateway/test_progress_edit_shared_clock_integration.py`; flood-control coverage in `tests/gateway/test_run_progress_interrupt.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/test_progress_edit_chat_throttle.py tests/gateway/test_progress_edit_shared_clock_integration.py tests/gateway/test_run_progress_interrupt.py`.
- **Rollback:** Remove `GatewayRunner._progress_edit_clock`, the shared-clock helpers and call-site stamps in `TurnRunner`, and the flood-control no-fallback branch. Remove only the patch-owned progress tests. Preserve unrelated gateway/session changes. Verify upstream coordinates the limit at `platform:chat_id` scope and does not fallback-send during a flood penalty.

### HERMES-006 — Resolve memory notifications per platform

- **Summary:** Uses the platform-specific display setting before the global fallback, allowing one platform to disable memory notifications without disabling them everywhere.
- **Surfaces:** `gateway/run.py`; `tests/gateway/test_memory_notifications_per_platform.py`.
- **Upstream tracking:** Narrow backport associated with upstream `#59364`.
- **Upstream PR:** Associated: #59364 (open; checked 2026-08-14).
- **Regression:** `pytest -q tests/gateway/test_memory_notifications_per_platform.py`.
- **Rollback:** Replace the `resolve_display_setting(...)` call with the released upstream configuration path and remove the private test only after equivalent per-platform precedence is covered upstream. Do not fall back to reading only `display.memory_notifications`.

### HERMES-007 — Keep interrupt sentinels out of API assistant text

- **Summary:** Preserves interrupt/completion state as API metadata while suppressing Hermes's internal “waiting for model” sentinel from assistant content and transcript messages.
- **Surfaces:** `gateway/platforms/api_server.py`; interrupt tests in `tests/gateway/test_session_api.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/gateway/test_session_api.py -k interrupt`.
- **Rollback:** Remove `_is_api_interrupt_sentinel` and `_api_final_response_text`, switch response construction to the released upstream representation, and remove/adapt only the two interrupt-metadata tests. Prove interrupted synchronous and streaming responses expose correct metadata without leaking the internal sentinel.

### HERMES-008 — Preserve Hindsight's explicit shared observation scope

- **Summary:** Preserves an explicit empty inner scope (`[[]]`) so Hindsight performs one shared consolidation pass instead of silently reverting to the combined default.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; `TestObservationScopes` coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** Related upstream issue `#74933`.
- **Upstream PR:** None after checked 2026-08-14; issue #74933 only.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k 'ObservationScopes or shared_scope'`.
- **Rollback:** Remove only the explicit-empty-inner-list preservation branch and its seven patch-owned tests after the released upstream parser proves equivalent handling for native and JSON forms, mixed scopes, whitespace-only entries, provider config, and retain calls.

### HERMES-009 — Fail Hindsight retains on extraction errors

- **Summary:** Sets `HINDSIGHT_API_FAIL_ON_EXTRACTION_ERRORS=true` for embedded profiles so collectors cannot mistake extraction failure for a legitimately empty successful document and advance source cursors.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; embedded-profile environment coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** No equivalent released upstream Hermes behavior was identified when this patch was published; also verify the current Hindsight server contract before retirement.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k embedded_profile_env` plus a failed-extraction operation probe against the supported embedded Hindsight version.
- **Rollback:** Remove the managed environment key only after upstream Hermes/Hindsight guarantees failed extraction yields a failed operation. Update the environment assertion and prove cursor-owning consumers still distinguish failure from an empty document.

### HERMES-010 — Avoid destructive Hindsight daemon restarts and empty-key overwrite

- **Summary:** Compares only Hermes-managed environment keys, ignores daemon-added keys, and preserves a stored API key when live secret resolution is temporarily empty. This avoids restarting the embedded daemon on every session initialization and killing in-flight work.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; embedded-profile drift coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k 'embedded and (env or config or restart)'` plus a daemon restart-count probe across repeated session initialization.
- **Rollback:** Replace the managed-key comparison and preserved-key materialization with the released upstream lifecycle implementation. Remove/adapt only its focused tests after proving daemon-added keys cause no restart and an unavailable secret lookup cannot blank a persisted credential.

### HERMES-011 — Serialize SessionDB reads, bound readers, and share one gateway database

- **Summary:** Routes the remaining unsafe public reads through `_read_ctx`, makes `GatewayRunner` reuse `SessionStore`'s database, and emits sanitized persistence diagnostics without unsafe retries. Upstream now owns the pooled-reader lifecycle and hard peak-connection permit; the private per-thread reader budget/reclamation implementation was removed during reconciliation rather than retained beside it.
- **Surfaces:** `hermes_state.py`; `gateway/run.py`; `run_agent.py`; `tests/test_sessiondb_cross_thread_safety.py`; `tests/gateway/test_runner_session_db_fd_budget.py`; persistence diagnostics in `tests/run_agent/test_run_agent.py`.
- **Upstream tracking:** The remaining private behavior combines the public-read, single-owner, and diagnostics contracts from upstream PRs `#73803` and `#78287`; deliberately excludes the fallback spool from `#78552`. Released upstream commits `87aedbe7b` and `0472c31aa` now provide the pooled reader lifecycle and hard peak budget.
- **Upstream PR:** Associated: #73803 and #78287 (open; checked 2026-08-14). Related but excluded: #78552.
- **Regression:** `pytest -q tests/test_sessiondb_cross_thread_safety.py tests/gateway/test_runner_session_db_fd_budget.py tests/run_agent/test_run_agent.py -k 'persistence or sqlite or session_db or reader or writer'`.
- **Rollback:** Remove only the remaining private public-read routing, shared gateway database ownership, and sanitized diagnostics in a follow-up change while preserving upstream's pooled-reader implementation and later unrelated edits. Before retirement, verify upstream covers all public-read serialization, single gateway DB ownership, sanitized diagnostics, and no duplicate-prone retry; upstream already owns the hard peak reader budget and cross-thread pooled drain.

### HERMES-012 — Retired GitHub Actions fork synchronization pipeline

- **Summary:** The former three-workflow GitHub Actions pipeline fetched upstream, rebased the private stack, tested a temporary candidate, and promoted an exact SHA. It remains retired. Unified Hermes job `maintain-targets` (`a6b63c34d53c`) now owns fleet-level scheduling and durable dispatcher receipts; hermes-agent is a heavy target that requires a dedicated repository-bound reconciliation rather than a second product-specific cron.
- **Surfaces:** Historical commits named in the index above; this lifecycle record. The live scheduler owner is external job `maintain-targets`; its dispatcher ledger and per-target receipts live outside this repository. This repository owns the hermes-agent patch contract and validator used by each dedicated reconciliation.
- **Upstream tracking:** This was fork-owner release machinery rather than an upstream product defect. The replacement remains Brian-owned and must continue to fail closed, test before publication, use an exact recorded lease, and keep runtime deployment separate.
- **Upstream PR:** None; fork-owner release machinery is not an upstream product contribution (checked 2026-08-14).
- **Regression:** Verify live job `a6b63c34d53c` is the only enabled `maintain-targets` owner, runs every 720 minutes in `America/Los_Angeles`, works from `/Users/brianle/dotfiles`, and records accepted per-target receipts before claiming another target. Verify hermes-agent is routed as a heavy dedicated case whose repository-bound run reads this manifest, preserves dirty work, runs affected and canonical gates plus independent review, uses an exact force-with-lease, proves remote readback, and never promotes a runtime. Verify the three retired workflow files and stale `automation/candidate/*` branches are absent.
- **Rollback:** Do not restore the retired workflows or recreate a `maintain-hermes-fork` job. If unified dispatch is defective, pause `maintain-targets` before its next run, leave fork `main` unchanged, repair the unified dispatcher/receipt owner, and prove one manual dedicated reconciliation plus remote readback before resuming it.

### HERMES-013 — Pin cron wall-clock schedules to per-job IANA timezones

- **Summary:** Adds an optional validated `timezone` field to cron jobs across persistence, scheduler calculations, tool/API/CLI/web/desktop surfaces, including update/clear recalculation, DST behavior, restart persistence, and legacy profile-timezone inheritance. Intervals and absolute one-shots retain their original semantics.
- **Surfaces:** `cron/jobs.py`; `tools/cronjob_tools.py`; gateway/CLI/web/desktop cron surfaces; `tests/cron/test_job_timezone.py` and associated cron UI/API tests.
- **Upstream tracking:** Issue `#26549`; open PR `#27393` superseded `#21926` but was incomplete for this explicit job-field contract when the patch was implemented.
- **Upstream PR:** Associated: #27393 (open) and superseded #21926 (closed; checked 2026-08-14).
- **Regression:** `pytest -q tests/cron/test_job_timezone.py tests/cron/test_cronjob_schema.py tests/cron/test_cron_script.py tests/gateway/test_api_server_jobs.py tests/hermes_cli/test_cron.py tests/hermes_cli/test_cron_interactive_timezone.py tests/hermes_cli/test_cron_parser_builder.py tests/hermes_cli/test_web_server_cron_profiles.py`; run the web and desktop cron model tests with their repository commands.
- **Rollback:** Inventory every persisted job with an explicit timezone. Migrate each to the released upstream representation or an equivalent profile/schedule arrangement before removing the private field. Then revert the stable-subject commit in a follow-up change, resolve current upstream overlap, remove duplicate UI/API/tool fields and fork-only tests, and prove New York/Los Angeles separation, profile fallback, update/clear behavior, DST, restart persistence, and interval/one-shot invariance against upstream.

### HERMES-014 — Cron CLI failure propagation

- **Summary:** Returns `cron_command(args)` from `cmd_cron` so nonzero cron subcommand results reach the top-level dispatcher and process exit status instead of being discarded as `None`.
- **Surfaces:** `hermes_cli/main.py`; `tests/hermes_cli/test_cron.py`.
- **Upstream tracking:** Current `upstream/main` still calls `cron_command(args)` without returning its result. Retire when a released upstream dispatcher propagates cron failures through an equivalent process-status contract.
- **Upstream PR:** None after checked 2026-08-14.
- **Regression:** `source venv/bin/activate && python -m pytest -q tests/hermes_cli/test_cron.py -k top_level_handler_propagates_failure_status`.
- **Rollback:** Once the released upstream dispatcher owns the same exit-status contract, remove the private `return` change and delete only `test_top_level_handler_propagates_failure_status` if upstream provides equivalent coverage. Run `tests/hermes_cli/test_cron.py`, invoke a deliberately failing read-only cron CLI operation, and verify its nonzero process status before promotion.

### HERMES-015 — Isolate gateway sessions from workdir cron cwd state

- **Summary:** Captures the gateway's configured cwd before cron execution begins, binds it into every interactive gateway turn, and makes prompt and tool cwd resolution prefer that session-scoped value over the mutable process-global `TERMINAL_CWD`. This prevents a concurrently running workdir cron from injecting its repository instructions or routing an unrelated gateway tool call into its project.
- **Surfaces:** `agent/runtime_cwd.py`; `gateway/run.py`; `gateway/slash_commands.py`; `gateway/runtime_footer.py`; `gateway/platforms/api_server.py`; `gateway/platforms/base.py`; cwd consumers in agent/tool modules; `tests/gateway/test_gateway_cron_cwd_isolation.py`.
- **Upstream tracking:** Issue `#81451`; PR `#81516` covers only sessions bound before the cron mutation and does not reproduce the observed cron-first ordering. PR `#61976` is directionally related but broader and not merge-ready.
- **Upstream PR:** Related: #81516 and #61976 (open; checked 2026-08-14).
- **Regression:** `scripts/run_tests.sh tests/gateway/test_gateway_cron_cwd_isolation.py tests/gateway/test_async_delivery_capability.py tests/agent/test_runtime_cwd.py tests/cron/test_cron_workdir.py tests/cron/test_terminal_cwd_lock.py tests/tools/test_file_tools_cwd_resolution.py tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py`.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated edits. Remove only HERMES-015's gateway baseline capture, ContextVar-aware cwd consumer changes, and dedicated regression. Before retirement, prove released upstream behavior under the cron-first ordering: hold a workdir cron in repository B, start a gateway session whose configured cwd is A, verify A's prompt/context/file/terminal/code-exec/delegation paths, and prove B's `AGENTS.md` never enters the gateway session.

### HERMES-016 — Preserve explicit flat MoA configuration during layered merges

- **Summary:** Detects an explicit legacy flat MoA preset in a user or managed configuration layer and removes only inherited named-preset selectors before the layer is merged. This lets the existing flat-config normalization path select the configured references and aggregator instead of silently using `DEFAULT_CONFIG` models.
- **Surfaces:** `hermes_cli/config.py`; `tests/hermes_cli/test_moa_config.py`.
- **Upstream tracking:** Issue `#82726`. Retire after an upstream release preserves or explicitly rejects flat MoA configuration at the complete `load_config()` boundary instead of silently substituting built-in models.
- **Upstream PR:** None after checked 2026-08-14; issue #82726 only.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_moa_config.py tests/hermes_cli/test_config.py tests/hermes_cli/test_config_loader_e2e.py tests/hermes_cli/test_config_validation.py tests/hermes_cli/test_config_read_guard.py -q` plus an isolated flat-config resolution probe using non-default model identifiers.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later config-loader changes, remove only the flat-MoA regression, and restore affected profiles to named `moa.presets.default` configuration before promotion. Do not return a runtime to flat configuration until released upstream behavior passes the same end-to-end resolution probe.

### HERMES-017 — Configure concise, distinct session titles

- **Summary:** Adds validated title-generation limits, sentence-case or title-case prompt selection, operator instructions, and canonical name aliases; deterministically enforces configured word/character caps; warns the title model away from recent session titles; and preserves the database's transactional uniqueness authority with one bounded distinct-title retry before the existing numbered fallback. A malformed or truncated auxiliary response must not replace the usable first-message-derived title: formatting-only output, bare or incomplete Markdown fences (including language labels), output with no Unicode letters or numbers, and `finish_reason=length` are rejected before persistence. Gemini-native fallback requests preserve JSON-schema output constraints and use a thinking-safe output budget. Compression continuations retain their intentional lineage naming.
- **Surfaces:** `agent/title_generator.py`; `agent/gemini_native_adapter.py`; `hermes_cli/config_defaults.py`; `hermes_state.py`; `cli-config.yaml.example`; `website/docs/user-guide/configuration.md`; `website/docs/user-guide/messaging/telegram.md`; focused title, Gemini-adapter, state, and auxiliary-config tests.
- **Upstream tracking:** Independent 2026-08-15 incident hypotheses were frozen before searching upstream: first, the permissive prose fallback accepted a bare Markdown fence and promoted it over the derived title; second, live replay proved the Google fallback discarded OpenAI `response_format`, spent the 64-token output ceiling on Gemini hidden thinking, and returned `finish_reason=length` with only a fence fragment. The correction belongs at the Gemini translation, title-budget, and title-response validation boundaries. Current upstream `main` at `30c469b15313711d47c45e7175d6ef5c8437f1ed` retains all three failure conditions. Issue `#83390` is the canonical related title-generation report; its PR cluster handles providers that reject `json_schema` and reasoning-budget exhaustion, but no issue or PR found translates OpenAI `response_format` into Gemini-native schema fields, rejects truncated auxiliary titles, or covers malformed-title persistence. Open source PR `#66353` supplies the title architecture but not this fallback hardening. Retire only after released upstream satisfies the complete contract.
- **Upstream PR:** Source: #66353. Associated partials: #85316 (provider-agnostic title reasoning control), #83725 and #83186 (response-format fallback ladders). Related: merged #39730 (Gemini native output defaults, not schema translation) and open #59825 (Gemini 2.5 thinking budgets). None satisfies the complete contract after checked 2026-08-15.
- **Regression:** `scripts/run_tests.sh tests/agent/test_title_generator.py tests/agent/test_gemini_native_adapter.py tests/test_hermes_state.py tests/hermes_cli/test_aux_config.py -q` plus a live Google-fallback replay proving native JSON-schema delivery, a non-truncated finish, and a complete title within the configured output budget.
- **Rollback:** Revert the stable-subject patches in follow-up commits while preserving later unrelated title/session changes. Rolling back only the root-cause hardening removes Gemini `response_format` translation, the thinking-safe title ceiling, `finish_reason=length` rejection, and their focused tests while retaining malformed-title sanitation. Rolling back the remaining 2026-08-15 sanitation removes only malformed-output and incomplete-fence validation plus their focused cases; do not disturb title configuration, precedence, uniqueness, or compression behavior. Full retirement additionally removes the remaining HERMES-017 configuration, prompt, normalization, recent-title, retry, and focused-test surfaces only after released upstream passes the complete contract.

### HERMES-018 — Select diverse semantic Telegram topic icons

- **Summary:** Opt-in semantic native Telegram topic icons resolve against the live allowed sticker set, honor exact emoji overrides, preserve observed manual choices, validate bindings immediately before mutation, and avoid the 24 most recently selected icons with durable per-chat least-recently-used history. For an eligible default-owned topic with a nonempty live allowlist, malformed or failed model selection falls back to a deterministic semantic/least-recent choice and logs the fallback reason; icon transport failure still never blocks session-title persistence or topic renaming. Post-response title/icon generation waits for completion but sends only the opening user request to any auxiliary provider; completed answer content never crosses that provider boundary.
- **Surfaces:** `agent/title_generator.py`; `gateway/run.py`; `hermes_state.py`; `plugins/platforms/telegram/adapter.py`; `website/docs/user-guide/messaging/telegram.md`; focused state, selector, adapter, and gateway tests.
- **Upstream tracking:** Independent 2026-08-15 incident hypothesis was frozen before searching upstream: the LLM icon chooser can return `None` after provider failure or an out-of-allowlist response, while the gateway silently performs a title-only rename. The correction belongs in the selector boundary: a deterministic allowed-list fallback plus warning-level reason logging, while preserving manual-icon ownership and title independence. Current upstream `main` at `30c469b15313711d47c45e7175d6ef5c8437f1ed` has no `choose_topic_icon`; open source PR `#66353` and related PR `#35737` retain model-only icon selection without this deterministic failure fallback.
- **Upstream PR:** Source: #66353; related: #35737 (both open; no deterministic failure fallback equivalent; checked 2026-08-15).
- **Regression:** `scripts/run_tests.sh tests/agent/test_title_generator.py tests/gateway/test_telegram_topic_mode.py tests/test_hermes_state.py tests/test_telegram_topic_status_ptb.py -q` plus one disposable live Telegram topic canary proving an eligible topic receives an allowed icon when model selection fails.
- **Rollback:** To roll back only the 2026-08-15 hardening, remove the deterministic fallback and warning reason while preserving live allowlist resolution, manual ownership, durable history, binding revalidation, and title-independent degradation. For full HERMES-018 retirement, disable `gateway.platforms.telegram.extra.auto_topic_icons` in every affected profile, verify title-only renaming, then remove the remaining selector, state, adapter, docs, and focused tests only after released upstream passes the complete contract.

### HERMES-034 — Add configurable Telegram Rich Message routing modes

- **Summary:** Adds `rich_messages: auto|always|never` to Telegram configuration. `auto` is the default adaptive route, `always` attempts Rich Messages for every final response that passes capability, client-risk, and size guards, and `never` forces legacy MarkdownV2. Existing booleans remain compatible: `true` maps to `auto`, `false` to `never`. Rich draft previews remain separately controlled by `rich_drafts`.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `hermes_cli/config_defaults.py`; `agent/system_prompt.py`; `cli-config.yaml.example`; Telegram messaging documentation; Rich Message tests.
- **Upstream tracking:** Fork-owned routing control. Current released upstream retains adaptive Rich Message delivery but does not provide the explicit `auto|always|never` contract after checked 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `uv run pytest -q tests/gateway/test_telegram_rich_messages.py tests/agent/test_system_prompt.py tests/gateway/test_config.py tests/gateway/test_telegram_rich_newlines.py tests/gateway/test_telegram_visual_spacing.py` — 117 passed. `git diff --check` passed.
- **Activation:** The active personal profile now has `gateway.platforms.telegram.extra.rich_drafts: true`. The running gateway could not self-restart; an external `hermes gateway restart` is still required. The new `always` mode is not active until the patched Hermes runtime is published and activated.
- **Rollback:** Set `rich_drafts: false`; set `rich_messages: true` or `auto` for adaptive routing; or revert the patch and restart externally. Do not force-push over the current fork/remote divergence.
- **Retirement:** Retire after released upstream exposes equivalent validated routing modes across config, system guidance, adapter behavior, backward-compatible booleans, and the focused Rich Message regressions.

### HERMES-035 — Add visible paragraph spacing to Telegram text delivery

- **Summary:** At the Telegram transport boundary, expands existing Markdown paragraph boundaries with an explicit non-breaking-space line so headings, prose sections, and action blocks remain visually separated on narrow clients. The normalization is idempotent, leaves single-line lists unchanged, and protects fenced code blocks and native pipe tables.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/gateway/test_telegram_visual_spacing.py`.
- **Upstream tracking:** Independent 2026-08-15 reproduction from Telegram screenshots showed ordinary Markdown blank lines rendering too tightly between report sections. The fix belongs at the channel renderer because cron agents expose `platform="cron"` before the scheduler selects a Telegram destination. No equivalent upstream implementation was identified in the maintained source during this change.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `uv run pytest -q tests/gateway/test_telegram_visual_spacing.py tests/gateway/test_telegram_rich_newlines.py tests/gateway/test_telegram_text_batching.py tests/gateway/test_text_batching.py tests/gateway/test_telegram_error_redaction.py tests/gateway/test_dm_topics.py` plus live Telegram readback of a representative multi-section report confirming visible spacing and no duplicate delivery.
- **Rollback:** Revert the stable-subject patch and its focused test, then verify ordinary Telegram Markdown delivery, rich-message tables/task lists, fenced code, chunking, and error fallback through the listed regressions. Runtime promotion remains separate from fork publication.
- **Retirement:** Retire after released upstream preserves equivalent visible paragraph spacing without changing single-line lists, fenced code, native tables, chunking, or fallback delivery, proven by the focused regressions and a live Telegram canary during separate promotion.

### HERMES-036 — Allow per-job local memory opt-in for cron

- **Summary:** Lets a cron job explicitly add the local file-backed `memory` toolset while cron continues to run with `skip_memory=True`, so external memory-provider hooks remain disabled.
- **Surfaces:** `cron/scheduler.py`; `tests/cron/test_scheduler.py`; `tests/agent/test_skip_memory_store_65429.py`; cron documentation.
- **Upstream tracking:** No equivalent released per-job local-memory opt-in was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/cron/test_scheduler.py tests/agent/test_skip_memory_store_65429.py -k 'memory_toolset or local_user_memory'`.
- **Rollback:** Remove only the explicit `memory` allowlist path and its focused tests; preserve cron's default memory-provider suppression and all unrelated per-job toolsets.
- **Retirement:** Retire after released upstream permits the same explicit local-memory toolset without invoking external provider hooks and passes the focused regressions.

### HERMES-037 — Explain decisions before interactive clarify prompts

- **Summary:** Keeps findings, terminology, trade-offs, and recommendations in normal assistant prose before a concise interactive question, and preserves that prose across gateway progress/prompt transitions.
- **Surfaces:** `tools/clarify_tool.py`; `gateway/run.py`; clarify, gateway-progress, and Codex-response tests; tool and Telegram documentation.
- **Upstream tracking:** No equivalent released cross-surface decision-context contract was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/tools/test_clarify_tool.py tests/gateway/test_run_progress_topics.py tests/run_agent/test_run_agent_codex_responses.py`.
- **Rollback:** Remove the explain-first schema guidance and gateway preservation branch together with their focused tests; preserve upstream multi-question, recommendation, and multi-select behavior.
- **Retirement:** Retire after released upstream preserves ordinary assistant decision context before clarify prompts on CLI and messaging surfaces.

### HERMES-038 — Show the Hindsight brain indicator only for retrieved context

- **Summary:** Uses the brain glyph only when Hindsight actually supplied retrieved context, avoiding a misleading provider indicator on turns without retrieval.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; Hindsight README; turn-context and Hindsight-provider tests.
- **Upstream tracking:** No equivalent released indicator contract was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/agent/test_turn_context.py tests/plugins/memory/test_hindsight_provider.py -k 'indicator or glyph or context'`.
- **Rollback:** Restore upstream's provider indicator behavior and remove only the focused glyph assertions.
- **Retirement:** Retire after released upstream exposes an equivalent truthful retrieved-context indicator.

### HERMES-039 — Keep memory retrieval guidance capability-aware and request-scoped

- **Summary:** Distinguishes semantic memory from transcript/session search, prefers curated observations for Hindsight recall, expands explicit recall context, and keeps provider-retrieved context scoped to the current request rather than mutating durable conversation history.
- **Surfaces:** `agent/memory_provider.py`; `agent/prompt_builder.py`; `agent/system_prompt.py`; `agent/conversation_loop.py`; `agent/turn_context.py`; `plugins/memory/hindsight/__init__.py`; `tools/session_search_tool.py`; focused prompt, sidecar, and Hindsight tests.
- **Upstream tracking:** No released upstream implementation satisfies the complete guidance, curation, expanded-context, and request-scoped sidecar contract through `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/agent/test_prompt_builder.py tests/agent/test_api_content_sidecar.py tests/agent/test_turn_context.py tests/plugins/memory/test_hindsight_provider.py`.
- **Rollback:** Remove the request-scoped retrieval sidecar and fork guidance/curation changes together; preserve upstream message alternation, prompt caching, and ordinary memory-provider lifecycle.
- **Retirement:** Retire after released upstream keeps retrieved provider context request-scoped and provides equivalent capability-aware semantic/transcript guidance and Hindsight curation.

### HERMES-040 — Extract eligible local Markdown images at the gateway boundary

- **Summary:** Detects eligible local Markdown image references in gateway output and routes them through the existing bounded native-media extraction path instead of leaving broken local links in delivered text.
- **Surfaces:** `gateway/platforms/base.py`; `tests/gateway/test_platform_base.py`.
- **Upstream tracking:** No equivalent released gateway extraction behavior was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_platform_base.py -k 'markdown and image'`.
- **Rollback:** Remove only local Markdown image extraction and its tests; preserve remote-image, attachment, path-validation, and media-size safeguards.
- **Retirement:** Retire after released upstream safely extracts equivalent local Markdown images at the delivery boundary.

### HERMES-041 — Fail closed on gateway-derived lifecycle control

- **Summary:** Treats any inherited gateway marker as tainted for self-control and blocks destructive launchctl verbs from gateway-origin terminal execution, preventing marker spoofing and supervisor kill loops.
- **Surfaces:** `hermes_cli/gateway.py`; `cron/lifecycle_guard.py`; `tests/cron/test_gateway_lifecycle_guard_launchctl.py`.
- **Upstream tracking:** No released upstream implementation satisfies both inherited-marker and destructive-launchctl contracts through `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/cron/test_gateway_lifecycle_guard_launchctl.py tests/hermes_cli/test_gateway_restart_loop.py`.
- **Rollback:** Remove only the inherited-marker hardening and launchctl verb classifier after another released mechanism prevents gateway self-termination; preserve ordinary external operator lifecycle commands.
- **Retirement:** Retire after released upstream fails closed on equivalent gateway-derived restart and launchctl self-control paths.

### HERMES-042 — Pin the supported Hindsight 0.9.1 client contract

- **Summary:** Pins `hindsight-client==0.9.1` and aligns provider adapters, lazy dependencies, packaging metadata, lock data, and documentation with that supported API.
- **Surfaces:** `pyproject.toml`; `uv.lock`; `tools/lazy_deps.py`; Hindsight provider source, metadata, docs, and tests.
- **Upstream tracking:** Upstream `5dd15872a6` still pins Hindsight client 0.6.1; no released equivalent 0.9.1 integration was identified on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `uv lock --check`; `scripts/run_tests.sh tests/plugins/memory/test_hindsight_provider.py tests/test_packaging_metadata.py`.
- **Rollback:** Revert the client pin and matching API adaptations as one unit; regenerate `uv.lock` with the repository-supported uv and preserve unrelated dependency updates.
- **Retirement:** Retire after released upstream supports the same or newer compatible Hindsight API and passes packaging plus provider regressions.

### HERMES-043 — Skip completed legacy startup resumes

- **Summary:** Prevents startup continuation recovery from re-running legacy sessions that already have a completed terminal response while preserving genuinely interrupted work.
- **Surfaces:** `gateway/session.py`; `tests/gateway/test_clean_shutdown_marker.py`.
- **Upstream tracking:** No equivalent released legacy-completion filter was identified through upstream `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_clean_shutdown_marker.py`.
- **Rollback:** Remove only the completed-legacy suppression and its focused cases; preserve current delivery-acknowledgement recovery and clean-shutdown semantics.
- **Retirement:** Retire after released upstream distinguishes completed legacy sessions from interrupted resumable work with equivalent regressions.

### HERMES-044 — Compose and protect final response transforms

- **Summary:** Composes output-transform hooks at the finalization boundary while preserving a substantive answer when a transform returns empty or non-substantive output.
- **Surfaces:** `agent/turn_finalizer.py`; `gateway/run.py`; `hermes_cli/lifecycle.py`; `hermes_cli/plugins.py`; Telegram adapter; transform-hook and Rich Message tests.
- **Upstream tracking:** No released upstream implementation satisfies the complete transform-composition and answer-preservation contract through `5dd15872a6` on 2026-08-18.
- **Upstream PR:** None after checked 2026-08-18.
- **Regression:** `scripts/run_tests.sh tests/test_transform_llm_output_hook.py tests/gateway/test_telegram_rich_messages.py`.
- **Rollback:** Remove the fork transform-composition path and its tests while preserving upstream finalization, plugin lifecycle, and Telegram rendering behavior.
- **Retirement:** Retire after released upstream composes equivalent transforms without permitting empty transform output to erase a substantive answer.

### HERMES-019 — Ignore hidden Slack thread-parent metadata updates

- **Summary:** Drops hidden `message_changed` events when Slack changed only thread-reply bookkeeping on an existing parent. This prevents a cold process cache from normalizing the old parent into a phantom user turn while preserving genuine visible edits and newly added mentions.
- **Surfaces:** `plugins/platforms/slack/adapter.py`; sanitized cold-restart incident and focused `message_changed` coverage in `tests/gateway/test_slack.py`.
- **Upstream tracking:** Open PR `#73450` identifies the same live replay path but is intentionally not cherry-picked because its broad classifier and test expansion are disproportionate to this patch contract. Retire when a released upstream implementation rejects equivalent hidden metadata-only parent updates with a cold cache while preserving visible edits.
- **Upstream PR:** Related: #73450 (open; checked 2026-08-14).
- **Regression:** `scripts/run_tests.sh tests/gateway/test_slack.py -k 'hidden_thread_parent or sanitized_lpg or message_edit_with_new_mention' -q`. The incident regression asserts the parent reaches neither routing nor persistence, cannot interrupt the active reply, and emits no busy acknowledgement.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated Slack adapter changes. Remove only the hidden parent-update classifier and its focused tests after released upstream passes the cold-cache metadata-only replay, visible text/block/file/attachment changes, malformed and partial snapshots, and edited-in mention cases.

## Upstream association and feedback contract

Every patch record must carry separate `Upstream tracking` and `Upstream PR` fields. The PR field must identify direct, source, associated, or merely related pull requests, or say `None after checked YYYY-MM-DD`. An issue is not a PR. A commit without a traceable PR is recorded as a commit, not silently promoted to a PR association.

On every maintenance run, and before publishing, promoting, or retiring a patch:

1. Resolve each linked issue, PR, commit, and released descendant against the live official repository. Check open and closed state, reviews, requested changes, unresolved threads, comments, CI, linked commits, merge/revert state, and release evidence.
2. Judge feedback against the patch contract, current source, reproductions, and executable tests. Maintainer authority, reviewer count, or approval state alone is not proof.
3. Classify each substantive item as valid, invalid, stale, already addressed, or requiring a narrower decision. Record the evidence for consequential classifications.
4. Apply valid feedback to the maintained fork implementation and focused regressions first. Then port that same verified correction to the associated upstream PR branch and read back both remote SHAs. Never let the public PR become a second, divergent implementation.
5. Block fork publication when valid feedback remains unresolved, when an upstream association is stale or ambiguous, or when the fork and its direct PR no longer implement the same behavior.
6. If released upstream satisfies the complete contract, retire the private implementation rather than retaining duplicate paths. A merged PR without released and verified equivalence is not enough.

## Adding or changing a patch

1. Load the canonical `hermes-patch` skill.
2. Add a provisional record here before implementation, including ID, summary, expected stable commit subject, upstream search, explicit upstream issue and PR associations (including a dated `None` result), regression, retirement condition, and rollback procedure.
3. Implement and verify the patch.
4. Update the record with final surfaces, tests, published commit identity, and live upstream issue/PR state.
5. If a direct upstream PR exists, inspect all current feedback and apply every valid item to the fork first, then update the PR from the verified fork correction.
6. Verify the manifest row is `Active`; run the repository maintenance-manifest validator during the dedicated hermes-agent reconciliation so duplicate IDs, unindexed records, missing stable subjects, upstream-association fields, and fork-only patch coverage block publication. The unified `maintain-targets` dispatcher records the accepted result; it is not a substitute for repository validation.
7. Ship the code and manifest together. A source patch without a complete record is not publishable.
8. On every upstream rebase, inspect patch equivalence and associated issue/PR feedback; never resolve a conflict by retaining both private and upstream implementations.

## Automatic synchronization

Unified Hermes job `maintain-targets` (`a6b63c34d53c`) runs every 720 minutes in `America/Los_Angeles` from `/Users/brianle/dotfiles`. The scheduler is only the trigger: the dispatcher ledger, immutable target snapshot, and accepted per-target receipts are authoritative. Each tick resumes one dispatcher run, processes at most three targets serially, and cannot claim the next target before recording the current target's receipt. Hermes-agent is classified as a heavy dedicated case. Its repository-bound reconciliation works from `/Users/brianle/Repos/hermes-agent`, fetches `origin` and `upstream`, reads this manifest, inventories installed and enabled plugins, resolves the live upstream issue/PR ledger and substantive feedback, rebases maintained `main`, compares conflicts against patch contracts, and checks released upstream for native patch or plugin replacements. It runs affected regressions, canonical gates, and an independent review of the exact rewritten delta. It may advance fork `main` only with the origin SHA fetched for that dedicated run as an explicit force-with-lease, followed by remote readback proving the verified candidate landed and contains upstream.

Neither unified dispatch nor the dedicated reconciliation may push to Nous Research, deploy or restart a runtime, uninstall plugins, edit plugin canonical-source repositories, or guess through an ambiguous conflict. A qualified native replacement must identify every affected profile and canonical owner plus the separately authorized promotion and complete-uninstall work required by “Plugin overlap and retirement.” A failed or ambiguous dedicated reconciliation must leave fork `origin/main` unchanged, restore or retain unrelated dirty work, and record the exact blocker or execution limitation in the unified dispatcher receipt. Runtime promotion remains a separate operation.

## Invariants

- Automation never pushes to `NousResearch/hermes-agent`.
- `main` moves only by explicit SHA and force-with-lease.
- Candidate verification never mutates Personal, LPG, or Meridian runtimes.
- Runtime promotion remains separate, with its own backup, canary, and rollback proof.
- A runtime rollback never rewrites the fork or changes another runtime.
- Patch retirement is behavioral: a clean Git apply/revert or matching commit message is not proof of upstream equivalence.
- Plugin retirement is also behavioral: a native replacement is not complete until its contract passes and the overlapping plugin, canonical source, configuration, dependencies, schedules, skills, and generated copies are removed from every affected profile during promotion.
- This manifest must describe every active Brian-owned patch; stale, missing, or non-actionable records block publication.

## Manual recovery

When synchronization is blocked:

1. Inspect the `maintain-targets` dispatcher run and accepted hermes-agent target receipt; distinguish a target blocker from a dispatcher, gateway, credential, tool, or host execution limitation.
2. Reproduce from `/Users/brianle/Repos/hermes-agent`.
3. Fetch `upstream/main` and rebase maintained `main` locally.
4. Resolve only after comparing current upstream behavior with every affected patch record above.
5. Remove a private implementation completely when upstream now satisfies its contract; do not layer both.
6. Run patch-specific regressions and full gates.
7. Push repaired `main` to `origin` using the remote SHA observed before reconciliation as the force-with-lease value.
8. Close the alert only after remote readback and CI pass.
