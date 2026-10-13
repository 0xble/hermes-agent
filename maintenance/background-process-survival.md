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
  the live process without rerunning the command, and polls for its completion.
  A gone process without an exit file emits a `lost`
  completion with `survivor_lost` provenance. A checkpointed survivor whose PID
  start time positively mismatches (`reused`) is never adopted, supervised, or
  signalled: a recorded exit file is reported once; otherwise the stale entry and
  its run directory are discarded without a `lost` notice.
  Enqueueing is not delivery: pending completions stay checkpointed and replay
  after a crash. Recorded-result recovery is independent of a signalable PID and
  restores the gateway watcher before reporting. New log bytes use the ordinary
  watch-pattern and live-output ingestion hooks. Delivery is one flock-guarded
  marker transition,
  pending -> `claimed` (owner PID, start time, per-registry nonce) -> `delivered`.
  A consumer (CLI/TUI drain, quiet one-shot follow-up, gateway watcher) takes the
  exclusive claim BEFORE render/admission; only that owner may settle or release
  it. A known failed send/admission releases the claim and keeps the completion
  retryable. A peer
  facing a live owner's claim retains its offer; a claim whose owner is gone,
  recycled, or an exec'd predecessor (same PID, unknown nonce) is reclaimed.
  This is an owner-lifetime lease: slow live owners are never overtaken by a
  timeout. A crash before rendering/admission retries; a crash after external
  delivery but before its marker is written has an ambiguous outcome and may
  replay (the transport and marker are not one transaction). Delivered markers
  suppress restart replays. Shutdown fences survivor pollers before teardown.
  Run directories are pruned after acknowledged-result retention
  expires, and immediately on spawn failure. Log polls read only a bounded tail.
- `gateway_shutdown` (both restart and an operator gateway stop) leaves opted-in
  processes running. Explicit operator stop paths (`process.kill`, `cli.stop`,
  `slash.stop`, and `process.stop`) still terminate them. This is the D3 operator-stop decision.
- The terminal tool description limits restart survival to idempotent bounded
  work such as gates, tests, and builds, never external writes.

## Provenance and patches

- Fork patch identities: `background-process-cut-receipts` (PR3) and
  `terminal-background-survival` (PR4). They are stacked draft PRs in the
  `0xble/hermes-agent` fork; do not push to `NousResearch`.
- `tools/process_registry_survival.py` owns the file-backed wrapper and survivor
  polling; the registry facade only routes the opt-in and lifecycle boundaries.
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
