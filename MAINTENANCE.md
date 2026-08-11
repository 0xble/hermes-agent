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
| HERMES-001 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Serialize malformed `state.db` repair and invalidate stale schemas. |
| HERMES-002 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Make raw SQLite backup and quarantine connection-safe. |
| HERMES-003 | Active | `chore(local): carry Brian-owned working-tree patches into the fork` | Raise the file-descriptor soft limit safely. |
| HERMES-004 | Active | `chore(local): carry Brian-owned working-tree patches into the fork`; `fix(telegram): atomically reserve per-chat sends`; `fix(telegram): preserve bounded cooldown semantics` | Enforce a per-chat Telegram send cooldown. |
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
| HERMES-015 | Active | `fix(cwd): isolate gateway sessions from cron workdirs` | Keep a workdir cron's process-global cwd override out of concurrent gateway prompts and tools. |
| HERMES-016 | Active | `fix(config): preserve flat MoA settings during merge` | Prevent inherited default presets from shadowing explicit flat MoA configuration. |
| HERMES-017 | Active | `feat(titles): configure concise distinct session titles`; `feat(titles): configure session title casing` | Make title shape configurable while preserving durable, race-safe uniqueness. |
| HERMES-018 | Active | `feat(telegram): add semantic topic icons and robust auto-renames`; `feat(telegram): remember 24 recent topic icons` | Select live Telegram topic icons without repeating the 24 most recent choices or overwriting manual icons. |
| HERMES-019 | Active | `fix(slack): ignore hidden thread-parent metadata updates` | Prevent Slack reply bookkeeping from replaying an old thread parent as a fresh user turn after a gateway restart. |

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

- **Summary:** Inside `TelegramAdapter`, atomically reserves a per-chat slot immediately before every persistent message-delivery Bot API call, including rich messages, every chunk and fallback attempt, control messages, and native media. Telegram `RetryAfter` deadlines advance the same shared clock; lock acquisition plus cooldown waiting share a bounded budget; control boundaries preserve retry metadata; positively identified pre-send connection/pool timeouts do not consume a slot; and idle chat state is pruned. Standalone CLI/cron sends and draft/edit/typing APIs are outside this process-local contract.
- **Surfaces:** `plugins/platforms/telegram/adapter.py`; `tests/test_telegram_send_cooldown.py`.
- **Upstream tracking:** Related upstream pull request `#66722` remains open and unmerged.
- **Regression:** `pytest -q tests/test_telegram_send_cooldown.py`.
- **Rollback:** Remove `_TelegramSendCooldownExceeded`, the per-chat cooldown state maps and bound, `_send_cooldown_seconds`, `_send_cooldown_max_wait`, the atomic send helper and its call sites, then remove the dedicated test. Verify the upstream adapter atomically coordinates concurrent rich, chunked, fallback, control, and media calls per chat, shares `RetryAfter` deadlines, bounds excessive waits, and prunes idle state before deploying the removal.

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

### HERMES-015 — Isolate gateway sessions from workdir cron cwd state

- **Summary:** Captures the gateway's configured cwd before cron execution begins, binds it into every interactive gateway turn, and makes prompt and tool cwd resolution prefer that session-scoped value over the mutable process-global `TERMINAL_CWD`. This prevents a concurrently running workdir cron from injecting its repository instructions or routing an unrelated gateway tool call into its project.
- **Surfaces:** `agent/runtime_cwd.py`; `gateway/run.py`; `gateway/slash_commands.py`; `gateway/runtime_footer.py`; `gateway/platforms/api_server.py`; `gateway/platforms/base.py`; cwd consumers in agent/tool modules; `tests/gateway/test_gateway_cron_cwd_isolation.py`.
- **Upstream tracking:** Issue `#81451`; PR `#81516` covers only sessions bound before the cron mutation and does not reproduce the observed cron-first ordering. PR `#61976` is directionally related but broader and not merge-ready.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_gateway_cron_cwd_isolation.py tests/gateway/test_async_delivery_capability.py tests/agent/test_runtime_cwd.py tests/cron/test_cron_workdir.py tests/cron/test_terminal_cwd_lock.py tests/tools/test_file_tools_cwd_resolution.py tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py`.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated edits. Remove only HERMES-015's gateway baseline capture, ContextVar-aware cwd consumer changes, and dedicated regression. Before retirement, prove released upstream behavior under the cron-first ordering: hold a workdir cron in repository B, start a gateway session whose configured cwd is A, verify A's prompt/context/file/terminal/code-exec/delegation paths, and prove B's `AGENTS.md` never enters the gateway session.

### HERMES-016 — Preserve explicit flat MoA configuration during layered merges

- **Summary:** Detects an explicit legacy flat MoA preset in a user or managed configuration layer and removes only inherited named-preset selectors before the layer is merged. This lets the existing flat-config normalization path select the configured references and aggregator instead of silently using `DEFAULT_CONFIG` models.
- **Surfaces:** `hermes_cli/config.py`; `tests/hermes_cli/test_moa_config.py`.
- **Upstream tracking:** Issue `#82726`. Retire after an upstream release preserves or explicitly rejects flat MoA configuration at the complete `load_config()` boundary instead of silently substituting built-in models.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_moa_config.py tests/hermes_cli/test_config.py tests/hermes_cli/test_config_loader_e2e.py tests/hermes_cli/test_config_validation.py tests/hermes_cli/test_config_read_guard.py -q` plus an isolated flat-config resolution probe using non-default model identifiers.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later config-loader changes, remove only the flat-MoA regression, and restore affected profiles to named `moa.presets.default` configuration before promotion. Do not return a runtime to flat configuration until released upstream behavior passes the same end-to-end resolution probe.

### HERMES-017 — Configure concise, distinct session titles

- **Summary:** Adds validated title-generation limits, sentence-case or title-case prompt selection, operator instructions, and canonical name aliases; deterministically enforces configured word/character caps; warns the title model away from recent session titles; and preserves the database's transactional uniqueness authority with one bounded distinct-title retry before the existing numbered fallback. Compression continuations retain their intentional lineage naming.
- **Surfaces:** `agent/title_generator.py`; `hermes_cli/config_defaults.py`; `hermes_state.py`; `cli-config.yaml.example`; `website/docs/user-guide/configuration.md`; `website/docs/user-guide/messaging/telegram.md`; focused title, state, and auxiliary-config tests.
- **Upstream tracking:** Cherry-picks the title commit from open PR `#66353`, then hardens it with backward-compatible defaults, operator instructions, deterministic word enforcement, normalized recent-title avoidance, and bounded collision retry. Retire only after released upstream satisfies that complete contract.
- **Regression:** `scripts/run_tests.sh tests/agent/test_title_generator.py tests/test_hermes_state.py tests/hermes_cli/test_aux_config.py -q` plus a clean-profile end-to-end title-generation probe proving configured limits and collision handling.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated title/session changes. Remove only HERMES-017 configuration fields, prompt construction, deterministic normalization, recent-title query, bounded retry, and focused tests. Restore affected profiles to upstream-supported title configuration before promotion, then prove released upstream still preserves manual-title precedence, exact transactional uniqueness, compression lineage, and configured title shape.

### HERMES-018 — Select diverse semantic Telegram topic icons

- **Summary:** Opt-in semantic native Telegram topic icons resolve against the live allowed sticker set, honor exact emoji overrides, preserve observed manual choices, validate bindings immediately before mutation, and avoid the 24 most recently selected icons with durable per-chat least-recently-used history. Icon failure never blocks session-title persistence or topic renaming.
- **Surfaces:** `agent/title_generator.py`; `gateway/run.py`; `hermes_state.py`; `plugins/platforms/telegram/adapter.py`; `website/docs/user-guide/messaging/telegram.md`; focused state, selector, adapter, and gateway tests.
- **Upstream tracking:** Cherry-picks the icon commit from open PR `#66353`, then replaces process-local-only ownership/diversity with durable `state.db` state and deterministic least-recent reuse. PR `#35737` hardcodes one account's icon IDs and couples Telegram metadata to the generic title callback. Retire only after released upstream uses a live allowlist, preserves manual ownership across restarts, rechecks topic/session authority, and degrades without losing titles.
- **Regression:** `scripts/run_tests.sh tests/agent/test_title_generator.py tests/gateway/test_telegram_topic_mode.py tests/test_hermes_state.py tests/test_telegram_topic_status_ptb.py -q` plus one disposable live Telegram topic canary before activation.
- **Rollback:** Disable `gateway.platforms.telegram.extra.auto_topic_icons` in every affected profile, verify title-only topic renaming, then revert the stable-subject patch in a follow-up commit while preserving unrelated Telegram/state changes. Remove only HERMES-018's derived state, selector, adapter methods, docs, and tests after released upstream passes the same live-set, restart, manual-preservation, race, and failure-degradation contract.

### HERMES-019 — Ignore hidden Slack thread-parent metadata updates

- **Summary:** Drops hidden `message_changed` events when Slack changed only thread-reply bookkeeping on an existing parent. This prevents a cold process cache from normalizing the old parent into a phantom user turn while preserving genuine visible edits and newly added mentions.
- **Surfaces:** `plugins/platforms/slack/adapter.py`; sanitized cold-restart incident and focused `message_changed` coverage in `tests/gateway/test_slack.py`.
- **Upstream tracking:** Open PR `#73450` identifies the same live replay path but is intentionally not cherry-picked because its broad classifier and test expansion are disproportionate to this patch contract. Retire when a released upstream implementation rejects equivalent hidden metadata-only parent updates with a cold cache while preserving visible edits.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_slack.py -k 'hidden_thread_parent or sanitized_lpg or message_edit_with_new_mention' -q`. The incident regression asserts the parent reaches neither routing nor persistence, cannot interrupt the active reply, and emits no busy acknowledgement.
- **Rollback:** Revert the stable-subject patch in a follow-up commit while preserving later unrelated Slack adapter changes. Remove only the hidden parent-update classifier and its focused tests after released upstream passes the cold-cache metadata-only replay, visible text/block/file/attachment changes, malformed and partial snapshots, and edited-in mention cases.

## Adding or changing a patch

1. Load the canonical `hermes-patch` skill.
2. Add a provisional record here before implementation, including ID, summary, expected stable commit subject, upstream search, regression, retirement condition, and rollback procedure.
3. Implement and verify the patch.
4. Update the record with final surfaces, tests, and the published commit identity.
5. Verify the manifest row is `Active`; the `maintain-hermes-fork` cron validates this index, its records, active stable subjects, and fork-only patch coverage before publication.
6. Ship the code and manifest together. A source patch without a complete record is not publishable.
7. On every upstream rebase, inspect patch equivalence; never resolve a conflict by retaining both private and upstream implementations.

## Automatic synchronization

The Hermes cron `maintain-hermes-fork` runs daily at 09:20 America/New_York and can also be run manually. It never mutates the canonical checkout. In a fresh temporary clone it fetches `0xble/hermes-agent:main` and `NousResearch/hermes-agent:main`, reads this manifest, inventories the currently installed and enabled plugins from the live maintained profiles, rebases the maintained stack, compares conflicts against patch contracts, and checks newly released upstream behavior for native replacements of both fork patches and plugin-owned problem contracts. It runs affected regressions plus canonical tests and lint, and independently reviews the exact candidate. It may advance fork `main` only with an exact recorded `force-with-lease`, followed by remote readback proving the verified candidate landed and contains upstream.

The cron never pushes to Nous Research, never deploys, uninstalls plugins, edits plugin canonical-source repositories, or restarts a runtime, and never guesses through an ambiguous conflict. If a native replacement qualifies, its result must identify every affected profile and canonical owner plus the separate promotion and complete-uninstall work required by “Plugin overlap and retirement.” Failures must abort the isolated rebase, leave fork `main` unchanged, and deliver the exact blocker in the cron result.

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

1. Inspect the failed `maintain-hermes-fork` cron run and its delivered blocker.
2. Reproduce from `/Users/brianle/Repos/hermes-agent`.
3. Fetch `upstream/main` and rebase maintained `main` locally.
4. Resolve only after comparing current upstream behavior with every affected patch record above.
5. Remove a private implementation completely when upstream now satisfies its contract; do not layer both.
6. Run patch-specific regressions and full gates.
7. Push repaired `main` to `origin` using the remote SHA observed before reconciliation as the force-with-lease value.
8. Close the alert only after remote readback and CI pass.
