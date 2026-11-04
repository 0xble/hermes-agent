# Background process survival

Load this unit when changing terminal background-process checkpointing, gateway
shutdown cut receipts, restart adoption, or exactly-once completion delivery.

## Design decision: wrap only jobs that can outlive the gateway (C)

Ordinary attached local jobs use the base Popen/PTY path unchanged: no shell
wrapper, exit marker, `process-runs` directory, or checkpoint `exit_path`.
Popen's wait status is authoritative, including explicit exits 129–192 and
negative signal statuses. Kill uses the base local process-tree path.

Only `persist_on_release`, `survive_restart`, and the pre-existing detached
managed-worker handoff publish file-backed exit status. A recorded marker is
read **before** checking PID liveness/start-time reuse and settles the original
job without adopting or signalling the current PID or stopping its old scope.
An unrecorded ordinary checkpoint retains base PID-reuse discard/no-signal and
gone-scope reap/retry behavior. Missing status is never invented as success.

### Why ordinary jobs are not wrapped

Eight review rounds exposed ordinary-wrapper regressions in explicit high exit
codes versus negative signal status, simple-command exec/kill latency, login-rc
`noclobber`/`errexit`, shutdown draining and reader ownership, PID reuse/scope
reaping, and new run-directory retention. None is needed to preserve a detached
job's result. Design C removes that extra protocol instead of normalizing native
wait results or repairing ordinary lifecycle behavior around a wrapper.

## Exit-marker and restart-safe predicate audit

`has_exit_marker` == can-outlive-gateway collection capability (`exit_path`).
`is_restart_safe` means **only** explicit `survive_restart`; neither persistent
files nor a poller grant restart-safe lifecycle or delivery semantics.

| Owner / call sites | Predicate and boundary |
|---|---|
| `spawn_local`, `_spawn_local_survivor`, managed-worker output handoff | Only outliving jobs are wrapped. Ordinary pipe/PTY commands stay native. |
| Checkpoint write / recovery | Omit ordinary `exit_path`; read recorded markers before PID fate. No marker: retain base ordinary reuse/discard and scope reap/retry. |
| `_finish_recovered_exit`, `_poll_persistent_detached` | Ordinary/persistent missing status uses `checkpoint.exit_uncollected`; opt-in lost status uses `survivor_lost`. |
| `_file_backed`, `_refresh_detached_session`, `_sync_file_backed_result`, `poll` | File-reader routing is a marker capability, never a lifecycle exemption. |
| `_survivor_poller_loop`, `_collect_recorded_exit`, kill partition | Collect receipt first; only then consider unrecorded PID fate/signals. |
| `_survivor_poller_stopped`, `_finish_survivor_if_allowed`, `_finish_survivor`, `_publish_finished` | Only `is_restart_safe` completion publication is shutdown-fenced; other readers drain normally. |
| `kill_process`, `kill_all` | Survivor kill receipts/exemptions require `is_restart_safe`; persist-on-release keeps its separate base release policy. |
| Completion retention, claim/release/acknowledgement, drain, gateway watcher | Only explicit opt-ins acquire the delivered-marker lease. Notification opt-outs still settle without admission. |
| Directory pruning, path codecs, log reads | Resource handling only, not survival classification. No ordinary attached job creates artifacts to prune. |

Retained log-backed jobs serialize reads/decoder updates per session. Adoption
seeds at most `max_output_chars` bytes without live-output/watch callbacks and
preserves watch counters; live polling drains every new byte in bounded chunks.
Finished recovery reads only that historical tail, never a full replay.

## Required behavior

Fork patch identity: `terminal-background-survival`.

- Cut receipts are written before a `gateway_shutdown` signal or a bounded process
  sweep. They are additive JSON records and do not mutate the live session state.
- Cut receipts use their own age and count retention budget; they are not evicted
  by the normal completed-result cap.
- Only jobs that can outlive the gateway atomically publish exit markers.
  Ordinary attached jobs keep the base memory-bounded pipe/Popen path with no
  marker or run directory. Managed workers retain their output-log handoff.
  Only explicitly opted-in background commands claim restart-safe dispatch and
  exactly-once delivery. Ordinary jobs retain their existing lifecycle kill
  policy. Legacy detached jobs whose PID is gone are retained as completed
  results, with `exit_code=null`, an explicit
  `finished, exit status unknown` completion when notification was
  requested, and any available log tail. Preserve explicit notification opt-outs.
  No missing status is inferred to be success or silently pruned.
- Missing status (`null`/absent), `lost` completion provenance, and the
  `checkpoint.exit_uncollected` / `survivor_lost` sources are explicitly unknown,
  not success or a fabricated numeric failure. Concise gateway notices use a
  warning marker and retain the output tail. Both watcher branches deliver
  unknown outcomes in `error` mode, including busy-turn receipts; `off` stays off.
  CLI/TUI shared transcript titles and model notices, classic CLI and React TUI
  docks, and desktop status rows/native notices must not paint unknown as green.
  Telegram and Slack use the shared gateway formatter/filter, not adapter forks.
- A `persist_on_release` notify job can finish while agents drain after the
  gateway queue consumer stops. Keep that finished job's existing checkpoint and
  exit-marker/log result through shutdown so the successor restores its route,
  enqueues the collected outcome and tail, and removes the one-startup handoff.
  Pending drain handoffs are protected from finished-result TTL/cap pruning only
  while the shutdown fence is set; a long-running job can exceed TTL when it exits.
  This reuses the checkpoint-backed saved-result recovery path, not a new outbox
  or a `survive_restart` lease: no new running-job exemption, delivered marker,
  or exactly-once promise for ordinary persistent jobs. Pollers still drain in
  the old gateway; shutdown fencing must not add synchronous receipt IO.
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
  retryable. TUI admission failures also re-queue the original completion and
  clear its UI dedup key; UI dedup never bypasses the survivor lease on later
  attempts. Text-only watcher sends retry every non-delivery while the gateway
  lives: a known failed send, a raised send, and an adapter or route that is
  absent right now (reconnecting or failed platform). Delays are exponential (at
  least one second, capped at 15 minutes) and the claim is released between
  attempts. Only a delivered send settles. Shutdown cancellation leaves the completion
  checkpointed for a successor. An adapter returning no send receipt has an unknown
  outcome, not a known failure; it is settled without retry to avoid duplicate
  sends. Both agent-notify and watcher-routed survivors remain checkpointed and exempt from result
  pruning until delivered. A peer
  facing a live owner's claim retains its offer; a claim whose owner is gone,
  recycled, or an exec'd predecessor (same PID, unknown nonce) is reclaimed.
  This is an owner-lifetime lease: slow live owners are never overtaken by a
  timeout. A crash before rendering/admission retries; a crash after external
  delivery but before its marker is written has an ambiguous outcome and may
  replay (the transport and marker are not one transaction). Delivered markers
  suppress restart replays. Shutdown fences only `survive_restart` pollers before
  teardown. Ordinary pipe readers and `persist_on_release` file-backed pollers
  keep finalizing during the agent drain. Fenced survivor queries do not wait
  on a poller that shutdown intentionally stopped. Kill paths collect a published exit marker before PID
  checks, cut receipts, or signals, preserving the real exit and completion rather
  than labelling an already-completed job `killed`.
  Run directories are pruned after acknowledged-result retention
  expires, and immediately on spawn failure. Directory sweeps are throttled to
  at most once per minute per profile to bound registry-lock IO; in-memory result
  expiry still runs normally. Preserve pending survivor markers. No ordinary
  attached artifacts exist, so no ordinary directory inventory is needed.
  Recovery reads a bounded
  historical tail without watch/live callbacks; live polling drains all new
  bytes in bounded chunks into a memory-bounded tail.
- Login rc options must not defeat file-backed supervision. Append to the
  pre-created output log under `noclobber`, and use an outer EXIT trap so
  `errexit` cannot bypass atomic exit publication without changing command
  semantics. Force-overwrite only the wrapper's private temporary exit marker
  before rename (including the managed-worker handoff). Native bash/zsh login-rc
  tests verify output and status before any parent wait-status fallback.
- The owning profile's effective `display.background_process_notifications: off`
  suppresses gateway process completion turns, including explicit
  `notify_on_complete=true` watchers. A suppressed survivor settles its delivery
  marker without admission or a public send, so recovery does not replay it.
  The retained process result remains available to `process(wait/log)`.
- `gateway_shutdown` (both restart and an operator gateway stop) leaves opted-in
  processes running. Explicit operator stop paths (`process.kill`, `cli.stop`,
  `slash.stop`, and `process.stop`) still terminate them. This is the D3 operator-stop decision.
- The terminal tool description limits restart survival to idempotent bounded
  work such as gates, tests, and builds, never external writes.
- Local non-PTY `persist_on_release` jobs share the survivor wrapper's
  file-backed `out.log` and exit file but none of its restart-safe dispatch
  or delivery lease. A job that outlives the gateway that spawned it must never
  write into that gateway's pipe: once the old gateway exits, the re-adopted job
  dies of `BrokenPipeError` on its next write. Re-adoption polls the files like a
  survivor. Shutdown kill semantics for `persist_on_release` are unchanged, and
  the run directory is pruned together with the finished session.
- Post-kill verification takes its verdict only from a probe that started after
  the settle window closed. On a loaded macOS host, the psutil ppid scan alone
  can outlast the window, and its "alive" answer predates the reap it waited for.

## Provenance and patches

- Fork patch identity: `background-process-cut-receipts` (PR3, merged fork PR #538).
- Fork patch identity: `terminal-background-survival` (PR4, draft fork PR #534).
  Track both units in the `0xble/hermes-agent` fork; do not push to `NousResearch`.
- `tools/process_registry_survival.py` owns the file-backed wrapper and survivor
  polling; the registry facade only routes the opt-in and lifecycle boundaries.
- Follow-up cut-receipt finalization fixes keep final receipts authoritative,
  exclude `cut_pending` from terminal-result loading, mark cut receipts explicitly
  for retention, and write small bounded-sweep receipts before signalling within half
  the remaining budget, retrying any uncovered session once after the signals.
  Cut/final receipt lock acquisition and the final prune respect the bounded stop's
  deadline. Reader finalization claims the session under the registry lock, then
  persists outside it: an unrelated writer cannot prevent a sweep from selecting
  and signalling its live targets. A blocked final result remains tracked in memory;
  only its owner may publish completion or release finite-parent waiters. Bounded
  finalizers that skip persistence retain the terminal result in the finished map.
  A kill committed after the owner's write refreshes its receipt from current state under the result lock.
  The final prune checks its deadline throughout scanning, legacy classification,
  age deletion, and count deletion; unfinished retention work waits for a later prune.
  A busy writer may leave a receipt unwritten, but cannot hold up termination signals.
- Shutdown survival filtering precedes cut marking, receipt writes, deadline
  assignment, and signals. A surviving command keeps its normal result retention
  and finalization owner; only an explicit process/operator stop may finalize it
  as killed. Receipt finalization and the file-backed delivery claim are separate:
  the former publishes one completion offer, the latter fences consumer delivery.
  The shutdown fence protects only queue publication, never receipt IO. An owner
  blocked behind another result writer may finish persisting after teardown begins;
  it leaves a pending checkpoint and no old-gateway offer, so the successor delivers.
- Compatibility is additive: new checkpoint and receipt keys have safe absence
  behavior so an older release can roll back without a destructive migration.
- Do not touch `gateway/platforms/event.py`, `response_filters.py`, or
  `gateway/lifecycle_ledger.py`; those surfaces are owned elsewhere. Exit-status
  notification wording owns only its additive English fallback keys in locales.

## Verification

Contract tests: `tests/tools/test_process_registry_exit_marker_semantics.py` `tests/tools/test_process_registry_login_rc.py` `tests/tools/test_process_registry_shutdown_drain.py` `tests/tools/test_process_registry_survivor_boundaries.py` `tests/tools/test_unknown_process_exit_notifications.py` `tests/gateway/test_background_process_notifications.py`

Unknown-status renderer regressions also live in `tests/hermes_cli/test_process_dock.py`,
`ui-tui/src/__tests__/processDock.test.tsx`, and
`apps/desktop/src/store/composer-status.test.ts`. Exercise the Node workspace tests
alongside the pinned Python contracts when changing those projections.

Run the focused process-registry and terminal proof surfaces with the canonical
FIFO runner, then run Ruff and `scripts/check_fork_patches.py --source-only`.
The receipt-deadline regressions hold a real reader behind an unrelated result
writer while a bounded sweep kills a real child, then verify durable output and
one completion after release. `test_survivor_receipt_finalization.py` adds a real
survivor blocked behind the result lock, verifies shutdown can fence it without
waiting for IO, and checks successor delivery once after owner persistence. It
also checks a mixed bounded shutdown cuts only ordinary jobs and leaves the
survivor's exit receipt uncut. Legacy-inventory tests force expiry during
classification, age deletion, and count deletion, then verify a later prune repairs retention.
For PR4, verify a real local non-PTY process survives a gateway restart and that
its completion is observed once after repeated polls.

## Retirement and rollback

Retire the unit when upstream provides durable background-command checkpointing,
restart adoption, and exactly-once completion delivery with the same operator-stop
semantics. Roll back by reverting the two fork patches; additive checkpoint and
receipt files may remain and are ignored by older code.
