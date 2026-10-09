# Maintained fork patch register

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
