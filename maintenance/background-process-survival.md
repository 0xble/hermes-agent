# Background process survival

Load this unit when changing terminal background-process checkpointing, gateway
shutdown cut receipts, restart adoption, or exactly-once completion delivery.

## Required behavior

- Cut receipts are written before a `gateway_shutdown` signal or a bounded process
  sweep. They are additive JSON records and do not mutate the live session state.
- Cut receipts use their own age and count retention budget; they are not evicted
  by the normal completed-result cap.
- Only explicitly opted-in local non-PTY background commands may survive a
  gateway restart. The wrapper owns `out.log`, an exit file, and a delivered
  marker in a profile-local directory; it starts a new session and never carries
  raw credentials in a durable receipt.
- Boot adoption matches the checkpoint by container or owner identity, restores
  the live process without replaying a completion, and polls until exactly one
  completion is emitted. A gone process without an exit file emits one `lost`
  completion. The delivered marker makes restarts and concurrent pollers
  idempotent.
- `gateway_stop_all` leaves opted-in processes running. Only the dedicated
  kill-processes path terminates them. This is the D3 operator-stop decision.
- The terminal tool description says that background execution is for idempotent
  bounded work such as gates, tests, and builds, never external writes.

## Provenance and patches

- Fork patch identities: `background-process-cut-receipts` (PR3) and
  `terminal-background-survival` (PR4). They are stacked draft PRs in the
  `0xble/hermes-agent` fork; do not push to `NousResearch`.
- Follow-up cut-receipt finalization fixes keep final receipts authoritative,
  exclude `cut_pending` from terminal-result loading, mark cut receipts explicitly
  for retention, and keep bounded sweeps signal-first with small receipts.
- Compatibility is additive: new checkpoint and receipt keys have safe absence
  behavior so an older release can roll back without a destructive migration.
- Do not touch `gateway/platforms/event.py`, `response_filters.py`, locales, or
  `gateway/lifecycle_ledger.py`; those surfaces are owned elsewhere.

## Verification

Run the focused process-registry and terminal proof surfaces with the canonical
FIFO runner, then run Ruff and `scripts/check_fork_patches.py --source-only`.
For PR4, verify a real local non-PTY process survives a gateway restart and that
its completion is observed once after repeated polls.

## Retirement and rollback

Retire the unit when upstream provides durable background-command checkpointing,
restart adoption, and exactly-once completion delivery with the same operator-stop
semantics. Roll back by reverting the two fork patches; additive checkpoint and
receipt files may remain and are ignored by older code.
