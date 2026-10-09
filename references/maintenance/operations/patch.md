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
