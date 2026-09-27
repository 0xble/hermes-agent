# Nightly regression repairs

Load when changing process heartbeat delivery, state DB writability preflight and quarantine, or the native Copilot ACP compaction E2E.

## Required behavior

A heartbeat carries output written before it was armed and cannot follow the completion notice. A concurrent opener may quarantine a damaged state DB between a peer's `is_file` and `os.access` checks; a vanished file is not a read-only file. The native ACP E2E must actually summarize an old tool result through the ACP provider while preserving the active tail and validating subsequent prompt grounding.

## Provenance and patch

Fork patch identity: `nightly-regression-0926b`.

Fork nightly [run 36274304491](https://github.com/0xble/hermes-agent/actions/runs/36274304491) at `f25c047` failed all three tests. The earlier Copilot fix in [#182](https://github.com/0xble/hermes-agent/pull/182) correctly changed missing usage to `None`, but its 15,000-token test threshold triggered immediately after the first read with approximately 17,866 estimated tokens (fixed bridge and tool schemas included), when the default protected head and lean tail left no eligible middle. Compression returned `no_progress` before any summarizer call. The scenario now uses a 19,000-token trigger after older file pairs exist, leaves those pairs eligible, and protects the active tail; its assertions require a real ACP summarizer call and a valid grounded continuation. The prior eight-read pressure estimate in [Copilot ACP usage](copilot-acp-usage.md) was not sufficient evidence that the summarizer ran.

`preflight_db_writability` identified a file before another opener renamed it into quarantine; the later `os.access` saw a missing path and raised a false read-only error. It now lets the startup lock and path recheck handle that absence. `arm_heartbeat` advanced the output cursor to data already captured by the reader, permanently omitting the early output. Heartbeat emission now checks exit under the registry lock and rechecks before enqueue to preserve completion order without holding that lock across output transformation.

Upstream design checked against `NousResearch/hermes-agent` main `9bb2ea1b5c316578d00c826833f0ba01f09d1788` (root and area AGENTS plus CONTRIBUTING). Related open [upstream PR #122852](https://github.com/NousResearch/hermes-agent/pull/122852) independently fixes the early heartbeat cursor; this fork patch also guards the completion-order race. No upstream merge or release is claimed for it or for the two other repairs.

## Verification and retirement

Run `scripts/run_tests.sh --file-retries 0` on `tests/e2e/core/providers/test_native_copilot_acp.py` (Linux non-root), `tests/hermes_state/test_zeroed_state_db.py`, and `tests/tools/test_process_heartbeat.py` in a pinned Linux CI container. The forced quarantine preflight race and heartbeat ordering tests are event-synchronized. Run `scripts/check_fork_patches.py --repo . --source-only` with a clean HOME and HERMES_HOME after committing.

Retire the fork patch only after the accepted upstream release has equivalent behavior and all three focused tests pass without these local changes. Reverting a single aspect requires retaining its invariant test and reassessing this unit's scope; no stored-state migration is involved.
