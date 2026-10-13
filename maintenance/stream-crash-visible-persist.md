# Stream preview crash ownership

## Required behavior

After a crash during a provider stream, a resumed turn reuses its predecessor's
known editable platform message so only one complete answer remains visible and
normal completion persists that answer. Do not infer provider completion from
an arbitrary preview, suppress partial-turn recovery, or edit a reset/successor's
message. Preserve configured streaming edit cadence, tool progress, cleanup and
live-status behavior.

## Provenance and patches

- **Identity/status:** draft, incomplete fork patch `stream-crash-visible-persist`.
- **Base:** `origin/main` `87d06eebffb7152e77e98cc48b5eb17a9b7993fb`.
- **Surfaces:** `gateway/stream_recovery.py`, stream consumer/transport, turn runner;
  no `run_inbound.py` or `run_startup.py` changes.
- **Boundary:** write-ahead real-preview snapshot before transport I/O; bind the
  returned platform ID after ACK. Recovery keys include the original turn token,
  routing key and session ID. Ephemeral draft frames do not enter this journal.
- **Schema:** additive SQLite table, one latest wire observation per turn token;
  facts are transport observations, not completed assistant turns. Existing
  delivery-ledger connection ownership is retained. The journal writer explicitly
  updates `updated_at`; snapshots older than seven days are pruned on writes.
- **Regression:** added `stream_accepted_edit_unpersisted` crash param deliberately
  waits for an initial partial preview ACK before holding the complete edit.
  Assertions are unchanged; the original runtime xfail is not expanded or removed.
- **Known blocker:** original `stream_accepted_unpersisted` emits the entire text in
  one SSE chunk and its `any/hold_after` fault holds the *first send*, not an edit.
  SIGKILL loses the returned platform message ID. The original strict cell stays
  RED. Its assertion forbids an extra marked copy although its docstring allows
  an honest possible duplicate; do not claim this draft closes that gap.
- **Proof:** use `HERMES_PYTHON=<worktree>/.venv/bin/python
  HERMES_E2E_STRICT_ACCEPTANCE=1 bash scripts/run_tests.sh <one file>`.
  Original strict baseline: 1 failed, 57 deselected, 2 unmarked complete replies.
  Added edit-only case on origin/main production sources: 1 failed, 58 deselected,
  same duplicate shape with held `edit`. On patched sources: 1 passed; the five
  existing other crash cases plus the new edit case: 6 passed. Journal units:
  8 passed. Further conformance/ledger evidence belongs in the draft PR.
- **Retirement:** replace only when upstream preserves exact preview ownership
  across process death and safely handles ACK-lost first sends without discarding
  unsent/partial answers or weakening exactly-once assertions.
