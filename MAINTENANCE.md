# Maintained Hermes fork

This repository tracks `NousResearch/hermes-agent` while carrying a small set of Brian-owned patches. Official upstream remains authoritative for all unmodified Hermes code. Fork `main` is the last candidate that passed fork verification; runtime promotion is a separate operation.

## Non-negotiable patch lifecycle

Every Brian-owned core patch must have an active entry in this file **before it is published**. The entry must name its stable commit subject, summarize the behavior, identify upstream tracking, name regression evidence, and give a source-level rollback procedure. A patch is not complete merely because its commit appears in Git history. Retired entries remain in this file as historical lifecycle records even though their private code must be gone.

When official upstream releases behavior that satisfies a patch contract, the private implementation must be **completely retired in favor of upstream**. Do not keep both implementations, a compatibility shim, disabled private code, or duplicate fork-specific tests “just in case.” Inspect the upstream implementation, run this entry's regressions against it, remove the private code, adapt or delete duplicate tests, promote the upstream-backed candidate across every active runtime, and verify the behavior there. Git history is the rollback record.

Record the removal commit's stable subject on the `Retired` row so fork-only code history remains attributable. An upstream issue, pull request, merge, or similar-looking commit is not enough. Retirement requires equivalent released behavior proven against the patch contract. If upstream only partially covers the contract, narrow and re-document the remaining private patch rather than claiming retirement.

Stable commit subjects survive rebases and are the manifest keys. Resolve the current SHA from the fetched fork history instead of persisting a value that the next upstream rebase will invalidate.

## Maintained patch index

| ID | Status | Stable commit subject | Purpose |
| --- | --- | --- | --- |
| HERMES-001 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Serialize malformed `state.db` repair and invalidate stale schemas. |
| HERMES-002 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Make raw SQLite backup and quarantine connection-safe. |
| HERMES-003 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Raise the file-descriptor soft limit safely. |
| HERMES-004 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Enforce a per-chat Telegram send cooldown. |
| HERMES-005 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Share the progress-edit throttle per chat. |
| HERMES-006 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Resolve memory notifications per platform. |
| HERMES-007 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Keep interrupt sentinels out of API assistant text. |
| HERMES-008 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Preserve Hindsight's explicit shared observation scope. |
| HERMES-009 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Fail Hindsight retains on extraction errors. |
| HERMES-010 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Avoid destructive Hindsight daemon restarts and empty-key overwrite. |
| HERMES-011 | Active | `fix(state): serialize public reads, bound readers, one gateway SessionDB` | SessionDB concurrency, reader lifecycle, and gateway ownership. |
| HERMES-012 | Retired | `chore: automate maintained fork synchronization`; `fix: use fork-safe candidate verification`; `fix: promote only dispatched fork candidates`; `chore(fork): enforce maintained patch manifest`; `chore(fork): adopt root maintenance manifest`; `chore(fork): retire GitHub sync workflows` | Historical GitHub Actions synchronization pipeline, replaced by the `maintain-hermes-fork` Hermes cron. |
| HERMES-013 | Active | `feat(cron): support per-job timezones` | Explicit IANA timezone pins for individual cron jobs. |
| HERMES-014 | Active | `fix(cron): propagate CLI failures` | Return cron subcommand failure status through the top-level CLI dispatcher. |

The umbrella commit contains independently retireable fixes. Never revert it wholesale to retire one of HERMES-001 through HERMES-010.

## Patch records

### HERMES-001 — Serialize malformed `state.db` repair and invalidate stale schemas

- **Summary:** Serializes writable-schema repair across processes, re-probes after lock acquisition, and bumps SQLite's schema cookie after direct `sqlite_master` surgery. This prevents simultaneous repairers and stale prepared schemas from re-corrupting the database.
- **Surfaces:** `hermes_state.py`; `tests/test_state_db_malformed_repair.py`.
- **Upstream tracking:** Related upstream work was recorded as PRs `#69609` and `#71982`. Re-evaluate their released descendants rather than assuming title-level equivalence.
- **Regression:** `pytest -q tests/test_state_db_malformed_repair.py`.
- **Rollback:** In a follow-up commit, remove `_cross_process_repair_lock`, `_repair_state_db_schema_locked`, `_bump_schema_cookie`, their constants/imports, and their call sites while preserving unrelated `hermes_state.py` changes. Remove only the four patch-owned repair-lock/schema-cookie tests. Run the regression against the upstream replacement before promotion. Do not revert the umbrella commit.

### HERMES-002 — Make raw SQLite backup and quarantine connection-safe

- **Summary:** Holds `offline_file_access` across the live-connection check and byte-level copy/fingerprint operation, closing the check/use race that could cancel POSIX SQLite locks.
- **Surfaces:** `hermes_state.py`; `hermes_cli/kanban_db.py`; `tests/test_raw_copy_offline_guard.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Regression:** `pytest -q tests/test_raw_copy_offline_guard.py`.
- **Rollback:** Remove the `_copy_all`/`offline_file_access` guarded backup path in `hermes_state.py` and `_backup_corrupt_db_locked` guarded quarantine path in `hermes_cli/kanban_db.py`, then remove `tests/test_raw_copy_offline_guard.py`. Preserve HERMES-001 and all unrelated database-repair behavior. Verify the upstream replacement with the same live-connection race cases before deleting the private test.

### HERMES-003 — Raise the file-descriptor soft limit safely

- **Summary:** Best-effort raises `RLIMIT_NOFILE` to 8192 before CLI dispatch, never lowering an existing limit or exceeding a finite hard limit. It protects launchd-started gateways from the default 256-descriptor ceiling.
- **Surfaces:** `hermes_cli/main.py`; `tests/test_fd_soft_limit.py`.
- **Upstream tracking:** Related upstream issues were recorded as `#36899` and `#75269`.
- **Regression:** `pytest -q tests/test_fd_soft_limit.py` plus a fresh-process supervisor acceptance check of the gateway's effective soft limit.
- **Rollback:** Remove `_RLIMIT_NOFILE_TARGET`, `_raise_fd_soft_limit`, and the call at the start of `main()`, then remove `tests/test_fd_soft_limit.py`. Before promotion, prove upstream or supervisor configuration produces an adequate effective limit after a generated service reinstall; do not retire this merely because reader leaks improved.

### HERMES-004 — Enforce a per-chat Telegram send cooldown

- **Summary:** Applies a bounded per-chat minimum gap to every Telegram send path, including rich messages and chunked sends, and returns a retryable flood-control result instead of waiting for an extreme penalty.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/test_telegram_send_cooldown.py`.
- **Upstream tracking:** Related upstream issue `#66722`.
- **Regression:** `pytest -q tests/test_telegram_send_cooldown.py`.
- **Rollback:** Remove `_send_cooldown_until`, `_send_cooldown_seconds`, `_send_cooldown_max_wait`, and the cooldown/stamping blocks in `send()`, then remove the dedicated test. Verify the upstream sender globally coordinates concurrent paths per chat and bounds excessive waits before deploying the removal.

### HERMES-005 — Share the progress-edit throttle per chat

- **Summary:** Coordinates progress edits across sessions sharing a chat, claims throttle slots before API calls, bounds clock storage, and avoids issuing a fresh send while Telegram is already flood-limiting edits.
- **Surfaces:** `gateway/run.py`; `tests/test_progress_edit_chat_throttle.py`; `tests/gateway/test_progress_edit_shared_clock_integration.py`; flood-control coverage in `tests/gateway/test_run_progress_interrupt.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Regression:** `pytest -q tests/test_progress_edit_chat_throttle.py tests/gateway/test_progress_edit_shared_clock_integration.py tests/gateway/test_run_progress_interrupt.py`.
- **Rollback:** Remove `GatewayRunner._progress_edit_clock`, the shared-clock helpers and call-site stamps in `TurnRunner`, and the flood-control no-fallback branch. Remove only the patch-owned progress tests. Preserve unrelated gateway/session changes. Verify upstream coordinates the limit at `platform:chat_id` scope and does not fallback-send during a flood penalty.

### HERMES-006 — Resolve memory notifications per platform

- **Summary:** Uses the platform-specific display setting before the global fallback, allowing one platform to disable memory notifications without disabling them everywhere.
- **Surfaces:** `gateway/run.py`; `tests/gateway/test_memory_notifications_per_platform.py`.
- **Upstream tracking:** Narrow backport associated with upstream `#59364`.
- **Regression:** `pytest -q tests/gateway/test_memory_notifications_per_platform.py`.
- **Rollback:** Replace the `resolve_display_setting(...)` call with the released upstream configuration path and remove the private test only after equivalent per-platform precedence is covered upstream. Do not fall back to reading only `display.memory_notifications`.

### HERMES-007 — Keep interrupt sentinels out of API assistant text

- **Summary:** Preserves interrupt/completion state as API metadata while suppressing Hermes's internal “waiting for model” sentinel from assistant content and transcript messages.
- **Surfaces:** `gateway/platforms/api_server.py`; interrupt tests in `tests/gateway/test_session_api.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Regression:** `pytest -q tests/gateway/test_session_api.py -k interrupt`.
- **Rollback:** Remove `_is_api_interrupt_sentinel` and `_api_final_response_text`, switch response construction to the released upstream representation, and remove/adapt only the two interrupt-metadata tests. Prove interrupted synchronous and streaming responses expose correct metadata without leaking the internal sentinel.

### HERMES-008 — Preserve Hindsight's explicit shared observation scope

- **Summary:** Preserves an explicit empty inner scope (`[[]]`) so Hindsight performs one shared consolidation pass instead of silently reverting to the combined default.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; `TestObservationScopes` coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** Related upstream issue `#74933`.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k 'ObservationScopes or shared_scope'`.
- **Rollback:** Remove only the explicit-empty-inner-list preservation branch and its seven patch-owned tests after the released upstream parser proves equivalent handling for native and JSON forms, mixed scopes, whitespace-only entries, provider config, and retain calls.

### HERMES-009 — Fail Hindsight retains on extraction errors

- **Summary:** Sets `HINDSIGHT_API_FAIL_ON_EXTRACTION_ERRORS=true` for embedded profiles so collectors cannot mistake extraction failure for a legitimately empty successful document and advance source cursors.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; embedded-profile environment coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** No equivalent released upstream Hermes behavior was identified when this patch was published; also verify the current Hindsight server contract before retirement.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k embedded_profile_env` plus a failed-extraction operation probe against the supported embedded Hindsight version.
- **Rollback:** Remove the managed environment key only after upstream Hermes/Hindsight guarantees failed extraction yields a failed operation. Update the environment assertion and prove cursor-owning consumers still distinguish failure from an empty document.

### HERMES-010 — Avoid destructive Hindsight daemon restarts and empty-key overwrite

- **Summary:** Compares only Hermes-managed environment keys, ignores daemon-added keys, and preserves a stored API key when live secret resolution is temporarily empty. This avoids restarting the embedded daemon on every session initialization and killing in-flight work.
- **Surfaces:** `plugins/memory/hindsight/__init__.py`; embedded-profile drift coverage in `tests/plugins/memory/test_hindsight_provider.py`.
- **Upstream tracking:** No equivalent released upstream implementation was identified when this patch was published.
- **Regression:** `pytest -q tests/plugins/memory/test_hindsight_provider.py -k 'embedded and (env or config or restart)'` plus a daemon restart-count probe across repeated session initialization.
- **Rollback:** Replace the managed-key comparison and preserved-key materialization with the released upstream lifecycle implementation. Remove/adapt only its focused tests after proving daemon-added keys cause no restart and an unavailable secret lookup cannot blank a persisted credential.

### HERMES-011 — Serialize SessionDB reads, bound readers, and share one gateway database

- **Summary:** Routes public reads through `_read_ctx`, caps per-thread WAL readers, reclaims dead-thread readers, allows cross-thread close, disables SQLite statement caching defensively, makes `GatewayRunner` reuse `SessionStore`'s database, and emits sanitized persistence diagnostics without unsafe retries.
- **Surfaces:** `hermes_state.py`; `gateway/run.py`; `run_agent.py`; `tests/test_sessiondb_cross_thread_safety.py`; `tests/gateway/test_runner_session_db_fd_budget.py`; persistence diagnostics in `tests/run_agent/test_run_agent.py`.
- **Upstream tracking:** Combines the relevant contracts from upstream PRs `#73803` and `#78287`; deliberately excludes the fallback spool from `#78552`.
- **Regression:** `pytest -q tests/test_sessiondb_cross_thread_safety.py tests/gateway/test_runner_session_db_fd_budget.py tests/run_agent/test_run_agent.py -k 'persistence or sqlite or session_db or reader or writer'`.
- **Rollback:** Revert the stable-subject commit in a follow-up change, resolving against current upstream rather than rewriting history. Preserve any later unrelated edits in the three shared source files. Before promotion, verify upstream covers all public-read serialization, the hard reader budget and reclamation, cross-thread drain, single gateway DB ownership, disabled/otherwise-safe statement caching, sanitized diagnostics, and no duplicate-prone retry.

### HERMES-012 — Retired GitHub Actions fork synchronization pipeline

- **Summary:** The former three-workflow GitHub Actions pipeline fetched upstream, rebased the private stack, tested a temporary candidate, and promoted an exact SHA. It was retired because the `maintain-hermes-fork` Hermes cron now owns the same responsibility without a second scheduler or GitHub-owned promotion path.
- **Surfaces:** Historical commits named in the index above; this lifecycle record. The active replacement is the external Hermes cron named `maintain-hermes-fork`, not repository code.
- **Upstream tracking:** This was fork-owner release machinery rather than an upstream product defect. The replacement remains Brian-owned and must continue to fail closed, test before publication, use an exact recorded lease, and keep runtime deployment separate.
- **Regression:** Verify the live scheduler has exactly one enabled `maintain-hermes-fork` job; its prompt requires an isolated clone, this manifest, affected patch regressions, canonical tests and lint, independent review, exact-SHA force-with-lease, remote readback, and no runtime deployment. Verify the three retired workflow files and stale `automation/candidate/*` branches are absent.
- **Rollback:** Do not restore the retired workflows. If the cron is defective, pause it before its next run, leave fork `main` unchanged, repair or replace the single cron owner, and prove a manual isolated reconciliation plus remote readback before resuming it.

### HERMES-013 — Pin cron wall-clock schedules to per-job IANA timezones

- **Summary:** Adds an optional validated `timezone` field to cron jobs across persistence, scheduler calculations, tool/API/CLI/web/desktop surfaces, including update/clear recalculation, DST behavior, restart persistence, and legacy profile-timezone inheritance. Intervals and absolute one-shots retain their original semantics.
- **Surfaces:** `cron/jobs.py`; `tools/cronjob_tools.py`; gateway/CLI/web/desktop cron surfaces; `tests/cron/test_job_timezone.py` and associated cron UI/API tests.
- **Upstream tracking:** Issue `#26549`; open PR `#27393` superseded `#21926` but was incomplete for this explicit job-field contract when the patch was implemented.
- **Regression:** `pytest -q tests/cron/test_job_timezone.py tests/cron/test_cronjob_schema.py tests/cron/test_cron_script.py tests/gateway/test_api_server_jobs.py tests/hermes_cli/test_cron.py tests/hermes_cli/test_cron_interactive_timezone.py tests/hermes_cli/test_cron_parser_builder.py tests/hermes_cli/test_web_server_cron_profiles.py`; run the web and desktop cron model tests with their repository commands.
- **Rollback:** Inventory every persisted job with an explicit timezone. Migrate each to the released upstream representation or an equivalent profile/schedule arrangement before removing the private field. Then revert the stable-subject commit in a follow-up change, resolve current upstream overlap, remove duplicate UI/API/tool fields and fork-only tests, and prove New York/Los Angeles separation, profile fallback, update/clear behavior, DST, restart persistence, and interval/one-shot invariance against upstream.

### HERMES-014 — Cron CLI failure propagation

- **Summary:** Returns `cron_command(args)` from `cmd_cron` so nonzero cron subcommand results reach the top-level dispatcher and process exit status instead of being discarded as `None`.
- **Surfaces:** `hermes_cli/main.py`; `tests/hermes_cli/test_cron.py`.
- **Upstream tracking:** Current `upstream/main` still calls `cron_command(args)` without returning its result. Retire when a released upstream dispatcher propagates cron failures through an equivalent process-status contract.
- **Regression:** `source venv/bin/activate && python -m pytest -q tests/hermes_cli/test_cron.py -k top_level_handler_propagates_failure_status`.
- **Rollback:** Once the released upstream dispatcher owns the same exit-status contract, remove the private `return` change and delete only `test_top_level_handler_propagates_failure_status` if upstream provides equivalent coverage. Run `tests/hermes_cli/test_cron.py`, invoke a deliberately failing read-only cron CLI operation, and verify its nonzero process status before promotion.

## Adding or changing a patch

1. Load the canonical `hermes-patch` skill.
2. Add a provisional record here before implementation, including ID, summary, expected stable commit subject, upstream search, regression, retirement condition, and rollback procedure.
3. Implement and verify the patch.
4. Update the record with final surfaces, tests, and the published commit identity.
5. Verify the manifest row is `Active`; the `maintain-hermes-fork` cron validates this index, its records, active stable subjects, and fork-only patch coverage before publication.
6. Ship the code and manifest together. A source patch without a complete record is not publishable.
7. On every upstream rebase, inspect patch equivalence; never resolve a conflict by retaining both private and upstream implementations.

## Automatic synchronization

The Hermes cron `maintain-hermes-fork` runs daily at 09:20 America/New_York and can also be run manually. It never mutates the canonical checkout. In a fresh temporary clone it fetches `0xble/hermes-agent:main` and `NousResearch/hermes-agent:main`, reads this manifest, rebases the maintained stack, compares conflicts against patch contracts, runs affected regressions plus canonical tests and lint, and independently reviews the exact candidate. It may advance fork `main` only with an exact recorded `force-with-lease`, followed by remote readback proving the verified candidate landed and contains upstream.

The cron never pushes to Nous Research, never deploys or restarts a runtime, and never guesses through an ambiguous conflict. Failures must abort the isolated rebase, leave fork `main` unchanged, and deliver the exact blocker in the cron result.

## Invariants

- Automation never pushes to `NousResearch/hermes-agent`.
- `main` moves only by explicit SHA and force-with-lease.
- Candidate verification never mutates Personal, LPG, or Meridian runtimes.
- Runtime promotion remains separate, with its own backup, canary, and rollback proof.
- A runtime rollback never rewrites the fork or changes another runtime.
- Patch retirement is behavioral: a clean Git apply/revert or matching commit message is not proof of upstream equivalence.
- This manifest must describe every active Brian-owned patch; stale, missing, or non-actionable records block publication.

## Manual recovery

When synchronization is blocked:

1. Inspect the failed `maintain-hermes-fork` cron run and its delivered blocker.
2. Reproduce from `/Users/brianle/Repos/hermes-agent`.
3. Fetch `upstream/main` and rebase maintained `main` locally.
4. Resolve only after comparing current upstream behavior with every affected patch record above.
5. Remove a private implementation completely when upstream now satisfies its contract; do not layer both.
6. Run patch-specific regressions and full gates.
7. Push repaired `main` to `origin` using the remote SHA observed before reconciliation as the force-with-lease value.
8. Close the alert only after remote readback and CI pass.
