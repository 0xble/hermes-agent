# Maintained fork patch register

## react-tool-name

See [Reaction Tool Name](../../../maintenance/react-tool-name.md) for the contract, upstream findings, verification and retirement conditions.

## subagent-process-handoff-route

See [Subagent Process Handoff Route](../../../maintenance/subagent-process-handoff-route.md) for the contract, upstream findings, verification and retirement conditions.

## seamless-restart-s2: Isolated Launcher Environment Acknowledgement

- **Independent hypothesis (2026-10-24):** The supervised gateway matches the intended release, interpreter, cwd and exact generated `-I -c` bootstrap, but acknowledgement rejects its nonempty `PYTHONPATH`. PM deliberately exports the selected dependency paths at `pm/environments.py:554`, and the stderr wrapper passes them to its child. Process inspection reports that launch environment even though isolated Python ignores it and the bootstrap clears it before importing. This predicts deterministic acknowledgement failure with a PM-activated wrapper and success without inherited `PYTHONPATH`. A different failed identity predicate would falsify this diagnosis.
- **Contract and boundary:** Accept ignored `PYTHONPATH` only for the exact generated isolated bootstrap for the intended root. Keep rejecting ambient paths for the non-isolated `-m` entrypoint. Preserve supervisor, release readiness, interpreter, cwd, pointer and plist verification. Existing recovery must finish an already-issued matching reload without issuing another reload.
- **Source surfaces:** `hermes_cli/immutable_releases.py`; `tests/hermes_cli/test_release_ack_environment.py` (fixture reused from `tests/hermes_cli/test_immutable_release_transactions.py`). Reuses fork identity `seamless-restart-s2`.
- **Upstream disposition:** Fork-owned immutable transaction and acknowledgement implementation, not an upstream interface. No upstream replacement applies to this local boundary.
- **Verification:** `scripts/run_tests.sh tests/hermes_cli/test_release_ack_environment.py tests/hermes_cli/test_immutable_gateway_ack.py tests/hermes_cli/test_immutable_no_gateway_restart.py` passes all 11 tests. Run with disposable `HOME` and `HERMES_HOME` roots. Include inherited-path recovery and non-isolated rejection regressions. Broader reconciliation tests initially hit the real-home metadata guard in this checkout location, then exceeded the observation timeout with an isolated OS home. Real launchd integration remains separately gated by operator authorization.
- **Rollback and retirement:** Revert only this launcher environment qualification and its regression changes. No state format or dependency change. Retire with the owning immutable-release patch when upstream provides its complete observed-activation contract.

## attended-switch-notice-transition

- **Status:** Candidate.
- **Contract:** Announce an attended-model switch only when the session transitions to that route. Queued follow-ups and repeated API-boundary resolution on an already-selected attended model do not announce another switch. Preserve genuine idle-to-attended transitions, first-session silence, fallback ownership and idle return behavior.
- **Source surfaces:** `gateway/run_turn.py`, `gateway/run_turn_runner.py`, `tests/gateway/test_attended_turn_runner.py`.
- **Independent hypothesis (2026-10-25):** Turn-start resolution can compare a stale `last_resolved_model` snapshot even though the durable committed attended-route marker is already true. API-boundary resolution also announces a changed desired key without requiring a changed live route. Reuse the committed marker during turn-start staging and require a real model/provider/API-mode transition at the boundary. An intervening idle API call before the duplicate notice would falsify this incident explanation. The incident logs instead show the queued follow-up stayed on the attended model.
- **Upstream findings:** Local-only attended-routing overlay. No upstream implementation to backport, so the upstream discussion-search exception applies.
- **Verification:** `HERMES_PYTHON="$PWD/.venv/bin/python" bash scripts/run_tests.sh tests/gateway/test_attended_turn_runner.py tests/gateway/test_attended_model.py tests/gateway/test_attended_busy_activity.py tests/gateway/test_attended_session_opening.py tests/gateway/test_attend_command.py` passes: 287 tests, 0 failures. The new cases cover stale-model queued staging with committed active/inactive routes and no-op API-boundary resolutions with absent/stale decision keys.
- **Outbound budget:** Removes redundant uses of the existing persistent notice rail. Adds no API calls, retries, timers, limiters or platform-specific send path. Existing shared egress budgeting is unchanged and worst-case notice traffic cannot increase.
- **Rollback:** Revert the committed-route guard in turn-start resolution and the route-equality guard in API-boundary emission, plus their matching tests. No configuration, schema, dependency or state migration.
- **Retirement:** Remove when the shared routing owner derives switch announcements solely from committed route transitions and equivalent regressions pass upstream.

## slack-progress-card-recovery
- **Status:** Draft follow-up to the live recovery candidate, not landed. Shared outbound budget is not activated.
- **Contract:** Retry transient append failures on the existing stream, coalescing the latest projection even while tools are idle. Honor Retry-After. Retry a start only after a positive rate-limit rejection. Suppress ambiguous starts and SDK connection retries instead of creating a duplicate card or fallback. Permanent failures log and use human-readable tool labels, never executable previews. Route current SDK chat, reaction, thread status/title and file-publication writes through one shared workspace-token/channel and workspace-token/method budget.
- **Source surfaces:** `gateway/run_task_card_progress.py` (the single owner of task-card publication; `TurnRunner` and `GatewayTurnProgressMixin` no longer carry copies), `plugins/platforms/slack/adapter.py`, `plugins/platforms/slack/task_cards.py` (the ledger-owned stream start), `plugins/platforms/slack/outbound_budget.py`; [behavior record](../../../maintenance/slack-progress-card-recovery.md).
- **Root cause:** `_task_card_publish` latched `native_failed` after every unsuccessful SendResult, ignoring retryable transport failures. Base `fdcba2ab110d`; timeout logs immediately precede the fallback warning. A retryable failure that did not latch this flag would falsify the diagnosis.
- **Upstream findings:** Related [#137803](https://github.com/NousResearch/hermes-agent/pull/137803) handles explicit stream expiry, not ambiguous start or transient append timeouts. [#103197](https://github.com/NousResearch/hermes-agent/pull/103197) addressed incompatible payloads, a distinct cause. No matching transient recovery repair found in the scoped searches.
- **Verification:** Recovery regression against base production modules: 12 failed, exit 1. Recovery candidate: 12 passed. The lazy-install regression failed before correcting dependency rebinding (2 SDK clients dispatched together), then passed. Parent canonical runner with `HERMES_PYTHON=$PWD/.slack-budget-test-env/bin/python`, `--file-retries 0`, across the shared-budget, recovery, decline-fallback, progress-topic, destination-preflight, native-streaming, Slack adapter, send-retry, stream-final and transcript-echo suites: **10 files, 360 passed, 0 failed**, including 14 controlled-clock budget cases. The long-cooldown regression first failed by sleeping through a 3600-second server penalty, then passed after returning that penalty to the caller. Ruff passes on all 4 touched Python files. Earlier staged code-health check against `2156f8aea51e` reported 0 blocking and 0 advisory. Repository-wide static failures are inherited: 3 shebang violations, oversized AGENTS.md chains, and unrelated Ruff findings.
- **Outbound budget:** The shared SDK `api_call` boundary covers the Bolt app client, authenticated client and shallow copies for the same workspace bot token. All threads share a 2-second channel gap: 30 writes/min, below 75% of Slack's [1 message/sec/channel](https://docs.slack.dev/apis/web-api/rate-limits/) ceiling (45/min). `chat.startStream` and other current write methods use 4-second workspace/method gaps (15/min), at most 75% of the [startStream Tier 2 floor, 20/min](https://docs.slack.dev/reference/methods/chat.startStream/). `chat.appendStream` uses 0.8 seconds (75/min), 75% of its [Tier 4 floor, 100/min](https://docs.slack.dev/reference/methods/chat.appendStream/). Nonempty thread status and task-only cosmetic appends are shed under contention, preserving existing latest-projection recovery. Clears and actual output wait for ordinary admission, but server penalties above 10 seconds return Retry-After to the caller rather than blocking a live turn. Every SDK client's hidden retry handlers are disabled, so explicit transport retries re-enter the gate and ambiguous starts remain unreplayed. A real 429 applies Retry-After only to that workspace-token/method, doubles its gap for 60 seconds after cooldown, then returns to the baseline. This is an in-process gateway-client guarantee, not a cross-process or cross-token coordinator. Shared transport regression first failed on the old factory (two clients sent in the same instant). Current coverage exercises real SDK serialization/validation with a controlled clock and transport, including the SDK retry loop. Live mixed-traffic qualification and exact-candidate review remain required.
- **Rollback and retirement:** Revert this patch and matching presentation assertions. No schema, dependency or config change. Retire when upstream preserves the same retry, ambiguity and clean-fallback contract.
## mcp-verbatim-tool-labels

- **Status:** Candidate.
- **Contract:** MCP labels retain the exact callable tool name, including underscores, hyphens and case, in direct calls, bridge batches, progress lines and GUI payloads. Hosted connector phrases, account aliases and app-title formatting are unchanged.
- **Source surfaces:** `tools/tool_labels.py`, `agent/display.py` (example), `tests/tools/test_tool_labels.py`, `tests/gateway/test_telegram_progress_literals.py`.
- **Independent hypothesis (2026-10-23):** `_action_phrase` converts MCP underscores and case before every client receives the shared label. Keeping the raw MCP tool suffix in `ToolLabel.action` fixes the shared boundary without a renderer-specific special case or extension. A second formatter modifying the action after this boundary would falsify the hypothesis. Inspection found progress only lowercases the app and GUI clients consume `text` or `action` unchanged.
- **Upstream findings:** Guidance and label implementation inspected at `bf0203a794a07aac3ff764972ab9087baa4e4f4e`. The original shared-label change intentionally humanized MCP actions. This fork intentionally prefers exact callable identifiers. Issue search for `tool_labels` returned no results. Related open [#55094](https://github.com/NousResearch/hermes-agent/pull/55094) changes desktop fallback labels, not the shared bridge owner, and does not preserve tool underscores.
- **Verification:** `HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh tests/tools/test_tool_labels.py tests/agent/test_display.py tests/gateway/test_telegram_progress_literals.py -q --file-retries 0 -j 1` passes: 53 tests. New regression file fails before the fix: 7 failed, 1 passed. Telegram rich-message, MarkdownV2 and stream-event paths retain literal underscores.
- **Rollback:** Revert the MCP action assignment and its matching tests/examples. No schema, dependency, configuration or state migration.
- **Retirement:** Remove when upstream's shared MCP label owner preserves the complete callable suffix and its consumers retain the same contract.

## background-review-memory-delete: durable observer requirement

- **Status:** Candidate; not landed or activated.
- **Contract:** Owner opt-in may bypass staging only with a durable transaction observer registered for the current profile. Missing, old, or other-profile observers stage all destructive single/batch operations. Commit-time durability: a durable-path admission commits only if the observer snapshot used by that commit (`observe_memory_transaction`, under the registry lock, before writing) still contains a durable current-profile observer; otherwise `DurableMemoryObserverUnavailable` aborts before any write and the tool stages the op. Undo semantics stay unchanged.
- **Source surfaces:** `tools/memory_tool.py`, `tools/memory_transactions.py`; unit `maintenance/background-review-memory-delete.md`. The companion agents plugin declares `durable=True` on its existing registration.
- **Upstream findings:** Fork-only opt-in to upstream #105921; no matching durable-observer opt-in repair found.
- **Verification:** Red regression: 6 failed for remove/replace/batch with absent/non-durable observers; 6 failed for remove/replace/batch when the durable observer is unregistered or non-durably replaced between admission and commit. `scripts/run_tests.sh tests/tools/test_memory_tool.py tests/tools/test_memory_transactions.py tests/agent/test_background_review_memory_scope.py`.
- **Rollback:** Revert the observer declaration/check changes and their tests only; retain the existing opt-in/staging implementation. Reverting only the plugin declaration safely returns unattended destructive operations to staging.
- **Retirement:** Remove when upstream provides the complete opt-in plus profile-scoped durable rollback contract. Land Hermes before the plugin to avoid an unjournaled gap.

## memory-context-note-stripping

- **Status:** Candidate; not landed or activated.
- **Contract:** Strip exact current/legacy notes only; preserve unrelated same-prefix recalled facts and adjacent whitespace. Fence suppression is unchanged.
- **Source surfaces:** `agent/memory_manager.py::_INTERNAL_NOTE_RE`, `tests/agent/test_memory_provider.py`; unit `maintenance/memory-context-note-stripping.md`.
- **Upstream findings:** Adapted exact-note matching from yingliang-zhang's open NousResearch/hermes-agent#64421 at `7e16b919d113a870af7302e2e513f77f46f54298`, not its other provider/prefetch changes.
- **Verification:** Red regression: 2 failed on synthetic same-prefix passport/lease facts. `scripts/run_tests.sh tests/agent/test_memory_provider.py`.
- **Rollback:** Revert the exact-note matcher and its tests only; retain the separate background-delete gate repair.
- **Retirement:** Remove when a released upstream sanitizer satisfies the same preservation and exact-wrapper contract.

## auto-recovery-ladder-notice

- **Status:** Active; published candidate pending protected landing.
- **Summary:** Buffer the post-exhaustion provider-recovery ladder countdown and give-up lines instead of emitting them on the durable diagnostic-status rail while recovery is still in progress.
- **Source surfaces:** `agent/turn_recovery_autorecover.py`; `tests/agent/test_turn_recovery_autorecover.py`.
- **Contract:** A successful recovery clears the countdown with ordinary retry chatter. Terminal ladder exhaustion flushes the buffered cycle and give-up lines. The live diagnostic-wait rail remains available and interrupt behavior is unchanged. Logger warnings remain unchanged.
- **Upstream findings:** Upstream NousResearch/hermes-agent prior-art search found merged PR [#116353](https://github.com/NousResearch/hermes-agent/pull/116353), which introduced the bounded ladder, and merged PR [#124350](https://github.com/NousResearch/hermes-agent/pull/124350), which changed desktop wait presentation. No matching upstream issue or PR was found for buffering the durable countdown notice; the fork patch is a narrow adaptation of the existing retry-status buffer.
- **Verification:** `HOME=/Users/brianle/.hermes/cache/scratch/hermes-test-home HERMES_HOME=/Users/brianle/.hermes/cache/scratch/hermes-test-home scripts/run_tests.sh tests/agent/test_turn_recovery_autorecover.py tests/agent/test_retry_status_buffer.py -q` — 15 passed. Baseline candidate test with the regression assertions fails on `origin/main` (4 failures), while the patched candidate passes (5 + 10 tests). `scripts/check_fork_patches.py --repo . --source-only` — 0 problems.
- **Rollback:** Revert the landed patch commit, restoring direct countdown/give-up diagnostic emission and the previous regression expectations; no migration, schema, dependency, credential, or runtime-state changes.
- **Retirement:** Remove the fork-only buffering change when a released upstream implementation provides the complete contract and its equivalent regression coverage.

## state-db-lock-turn-survival

- **Status:** Active temporary upstream backport. Fork PR [#412](https://github.com/0xble/hermes-agent/pull/412).
- **Summary:** A SQLite lock on turn-lease refresh is a missed tick, bounded by the row's committed expiry, not a lost lease. A failed post-tool flush keeps `session_persistence_failed:<cause>` instead of being overwritten with `pending_tool_result`. Triggered by the 2026-10-09 07:04 PDT cron `d8ddc06f5259` failure during a bulk delete on a 38 GB `state.db`.
- **Source surfaces:** `agent/turn_facade_lease.py`; `hermes_state_compression.py`; `agent/turn_finalizer.py`; `tests/agent/test_turn_facade_lease.py`; `tests/agent/test_turn_finalizer_interrupt_alternation.py`. Unit: `maintenance/state-db-lock-turn-survival.md`.
- **Upstream findings:** A: merged [#134577](https://github.com/NousResearch/hermes-agent/pull/134577) (merge `c538ec5f402e`), commits `9ba015eafa5c`, `96e2fef9a745`, `064baef6471f`, `2d89e0fd781e`. B: open [#132559](https://github.com/NousResearch/hermes-agent/pull/132559) (shanelic), commits `cd2444653bde`, `857365582a8d`. Cherry-picked with `-x`; every backport commit's patch-id equals its upstream commit's, with no conflicts.
- **Verification:** The three upstream regressions fail on fork base `d2fa237e7078` (3 failed) and pass on the backport (3 passed). The 17 affected modules pass (169 tests). `scripts/check_fork_patches.py --repo . --source-only` reports 0 problems.
- **Rollback:** Revert the six backport commits and this record. No schema, migration, config, or dependency change; `session_turn_lease_expires_at` is a new read-only method.
- **Retirement:** A retires when fork sync #396 (`candidate/fork-sync-v0.21.6-20261008`) or a later sync lands. B retires when upstream merges #132559 and a sync includes it; if upstream changes it first, take upstream's version.

## state-db-compact-at-start

- **Status:** Active; landed on fork `main`, not promoted to any runtime.
- **Summary:** `hermes sessions optimize --at-next-start` records a one-shot request (`state.db.compact-at-start.json`) without opening the store, so it works beside a live gateway. The next gateway start runs the same FTS merge + VACUUM + TRUNCATE checkpoint as `hermes sessions optimize` before anything in the process opens state.db, logs before/after size at INFO, and clears the request.
- **Source surfaces:** `hermes_state_compaction.py`; `gateway/run.py` (`_run_requested_state_db_compaction`, called in `start_gateway` right after the PID-file claim); `hermes_cli/sessions_cmd.py`; `hermes_cli/subcommands/sessions.py`; `website/docs/user-guide/sessions.md`; `tests/hermes_state/test_compact_at_next_start.py`. Full record in `MAINTENANCE.md`.
- **Contract:** Runs only in the gateway that won the PID claim, before the control socket, adapters, cron, housekeeping or any SessionDB handle. Skips with a WARNING and keeps the request when a foreign process holds the store (`foreign_state_db_holders`), `release-txn.json` awaits this gateway's acknowledgement, or free disk is below about 2x live data + 1 GiB. Renews the startup-watchdog lease every 60s while progress is observable. Attempts are counted before the rewrite and capped at 3. Never raises into startup.
- **Upstream findings:** No upstream issue or PR proposes deferred or startup compaction. Related: #84525 (live-holder optimize, answered by the held-store refusal), #121324/#121783 (prune exempted from that guard), #57752/#112105 (auto-VACUUM gating).
- **Verification:** `scripts/run_tests.sh tests/hermes_state/test_compact_at_next_start.py` fails before (module absent) and passes after (11 tests); the related suites (held-store gate, auto-vacuum holder gate, startup lease, startup watchdog, runner startup, host lock, replace ownership) pass.
- **Rollback:** Revert the commit carrying `Fork-Patch: state-db-compact-at-start`; a leftover request file is inert.
- **Retirement:** Remove when upstream ships a supported way to compact a gateway-held store.

## gateway-loop-statedb-guard: generation checkpoint durability

- **Identity:** Reuses the existing `Fork-Patch: gateway-loop-statedb-guard` identity and
  [behavior record](../../../maintenance/gateway-loop-statedb-guard.md).
- **Summary and placement:** Repair the core `SessionDB` generation fence after fork PR #494,
  not an extension or seamless-restart change. The independent proposal checks the main-file
  header through the existing lock-safe probe and retries PASSIVE until the WAL-visible
  `application_id` is durable. Unknown probe results retry rather than declaring durability.
  Preserve #494's settled warm-open fast path and deleted-sidecar descriptor rechecks.
- **Source surfaces:** `hermes_state.py::_ensure_db_file_generation`;
  `tests/hermes_state/test_generation_checkpoint_durability.py` and the existing warm-open,
  generation, checkpoint, file-identity, deleted-WAL and process-registry coverage.
- **Baseline and upstream findings:** Fork base `881af4c2805230cad2dc10e539393f798114edc4`
  (#494); upstream inspected at `835c3dd038d903182a66ecc45e34bf5e0de65f00`. Governing guidance:
  upstream `AGENTS.md`, `CONTRIBUTING.md`, `tests/AGENTS.md` and contributing/package-management
  docs. Closed issue [#45383](https://github.com/NousResearch/hermes-agent/issues/45383) and merged
  [#99753](https://github.com/NousResearch/hermes-agent/pull/99753) establish generation durability
  and PASSIVE-only checkpointing. Closed [#101881](https://github.com/NousResearch/hermes-agent/pull/101881)
  was adopted through merged [#108067](https://github.com/NousResearch/hermes-agent/pull/108067):
  avoid no-op warm-open writes. Current upstream still checkpoints every generation open, so it
  does not have this fork-only regression. Open [#135066](https://github.com/NousResearch/hermes-agent/issues/135066)
  concerns broader maintenance-connection fencing, which this candidate does not change.
- **Verification:** Red-first real-SQLite regression: after a blocked initial checkpoint and a
  second warm open, the main-file header remained `0` and its generation row absent on the base.
  Both become durable with the repair, verified by a main-file-only copy without the WAL. The
  settled warm-open test still observes no checkpoint. Canonical `scripts/run_tests.sh` across
  8 affected files: **469 passed, 37 skipped**. Ruff, Windows-footgun, temporary-path and
  source-only fork-patch checks pass.
- **Stack fit:** Narrow core repair reusing the lock-safe header probe; no new plugin, sidecar,
  persisted marker, timeout, periodic checkpoint, schema or config.
- **Rollback and retirement:** Revert the durability commit to restore #494's opener-local gate.
  Retire when upstream's warm-open implementation both retries blocked initial checkpoints and
  skips settled checkpoints, verified by the same reader-held/main-file-only regression.

## cron-local-delivery-hint

- **Status:** Candidate; not landed or activated.
- **Contract:** When every comma-separated `deliver` token is `local` (missing or empty resolves to `local`), the cron preamble says the reply is saved to local output only and tells the run to perform any relay its instructions require. Other values, including mixed `local,origin`, keep the upstream hint. SILENT, FAILURE and RECURSION text stays byte-identical.
- **Source surfaces:** `cron/scheduler_prompt.py` (`_CRON_HINT_LOCAL`, `_cron_hint_for`), `tests/cron/test_cron_local_delivery_hint.py`; unit `maintenance/cron-local-delivery-hint.md`.
- **Upstream findings:** Closed, unmerged NousResearch/hermes-agent#44042 proposed a local-only variant. Open #127174 and #127185 add hint opt-outs instead, and open issue #69495 asks for a customizable preamble. Upstream `main` still uses the unconditional hint.
- **Verification:** Red regression: 6 failed on the base for local, missing, empty, list, repeated and padded `local` values. `scripts/run_tests.sh tests/cron/test_cron_local_delivery_hint.py tests/cron/test_scheduler.py tests/agent/test_synthetic_prompt.py`.
- **Rollback:** Revert the commit carrying `Fork-Patch: cron-local-delivery-hint`. No state or config migration is involved.
- **Retirement:** Remove when a released upstream hint distinguishes local-only delivery and allows instruction-required self-relay.
