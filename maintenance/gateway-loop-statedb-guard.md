# Gateway loop state.db guard

Load this unit when gateway async code reads or writes `state.db` or the session store, or when
changing what a gateway restart starts at once (restart auto-resume, cron catch-up), or changing
native active-conversation replay access paths in `hermes_state_messages.py`.

## Required behavior

- The gateway event-loop thread never waits on `state.db` or on the session-store lock, which a
  turn worker holds across its state.db write. [Seamless restart](seamless-restart.md) (PR 475)
  owns the per-turn, notification and topic-recovery hops. This unit adds the busy-path
  reaped-session probe (`_hm_reaped_session_id`, run through `asyncio.to_thread`).
- `hermes_loop_guard` keeps new loop-thread sites from slipping in. The gateway registers its
  loop thread when it binds the loop at startup and clears it when `stop()` disarms the
  liveness guards. The SessionDB writer lock and the session-store lock are `LoopGuardedLock`s
  and `SessionDB.__init__` checks too: a blocking use on the registered thread raises
  `LoopThreadBlockingError` under pytest and logs one WARNING per call site in production
  (never raises there). `/subgoal`, `/heartbeat` and `/loop` run their manager loads and writes
  through `_run_in_executor_with_context`. Sites the guard named on the live gateway also run off
  the loop: the stale Telegram topic-binding prune in `_send_with_retry`, the queued goal
  continuation's active-goal recheck, `/reasoning` (typed, picked, `/model --reasoning`) and the
  topic-session reasoning pick (session-store write), the process watcher's registry lookup
  (retained results open state.db), the startup-restore drain's session-key resolution (Telegram
  topic recovery), the boot resume-candidate snapshot, and the first open of each served secondary
  profile's handles (primed off the loop at startup and on hot-add).
- 2026-10-25 sweep of the sites the guard named on the live gateway (about 120 active agents,
  writer-lock holds of 3-13 s, two exit-75 kills): attended route resolution takes the durable
  activity stamp and `get_attended_route_active` from the off-loop resolution snapshot; turn
  assembly hydrates the persisted `/reasoning` pick through `_session_reasoning_override_async`
  and passes it to `_run_agent_build_turn_context`; `/status` awaits the fenced model-override
  rehydration; hygiene compression's provenance stamps (a forced activity write), failure-streak
  reads, cooldown writes and streak resets run in worker threads, including from the
  turn-hold done-callback and the unwind path, which schedule them as retained tasks; Telegram's
  in-send and control-send stale-topic prunes, delivery's automation topic-owner claim, `/retire`
  and topic-session binding peeks, and Slack thread hydration (active-session check, watermark
  read and write) run through `asyncio.to_thread`. Concurrent Slack replies in one thread may
  now persist watermarks out of order; a regressed watermark only re-injects already-seen replies
  after a restart. The off-loop `/reasoning` rehydration publishes the stored pick only while
  `ConversationState.reasoning_override_epoch` is unchanged (every write of the field and every
  conversation `clear()` bumps it), so a set, reset or boundary during the read wins. Cooldown
  writes scheduled from the done-callback and unwind paths are tracked per session id, and the
  hygiene gate waits for a pending one (bounded by `_HYGIENE_PENDING_COOLDOWN_WAIT_S`, 10 s)
  before reading the durable cooldown. With SQLite available, the failure-streak bump refreshes
  only a live session's in-memory view, never get-or-creates one from a worker thread. A role hold
  with attended routing off takes the same off-loop resolution snapshot. The Slack DM seed (`_seed_dm_session`, and the `_adopt_legacy_slack_entry`
  and compression-tip reads under it) belongs to PR #814 (`slack-seed-off-loop`).
- The pre-open deleted-WAL holder scan (host-wide libproc on macOS, already bounded at 5 s
  fail-open by upstream `233434b41447`; a cold scan took 77 s on the loaded host) reuses a
  clean verdict for 10 s while both sidecars keep their identity, never while one is missing,
  and runs once per path at a time; a timed-out scan is never cached.
- The cron-tick worktree prune (git classification plus `du` of `.worktrees/`) waits 30
  minutes after process start.
- A restart admits resumed turns `_STARTUP_RESUME_CONCURRENCY` (4) at a time. Each slot is held
  until the turn ends or `_STARTUP_RESUME_OPENING_S` (15 s) passes, so a long turn never holds
  later sessions. `cron.max_parallel_jobs` defaults to 4 (`0` = unbounded), which bounds the
  first tick's catch-up as well.
- The storage-health latch skips path resolution while nothing is latched, so every write no
  longer resolves the state.db path under a process-wide lock.
- A turn whose lease refresh stays blocked on a locked state.db past the row's expiry keeps
  running while a read shows the row still names it. It stops only on a real takeover. See
  [busy state.db turn survival](state-db-lock-turn-survival.md).
- The periodic PASSIVE WAL checkpoint runs on a dedicated connection without `SessionDB._lock`
  (one at a time). With `checkpoint_fullfsync` on macOS it waits for the whole device's dirty
  data; under the writer lock that stalled every writer (2026-10-11 10:36: 20 turns and 41
  threads queued behind one checkpoint). The connection is closed under the WAL lock guard in
  `close()`, before the writer, and pinned beside a retired writer. The periodic incremental FTS
  merge (`_merge_fts_incrementally`, every `_FTS_MERGE_EVERY_N_WRITES`) runs on the same
  maintenance connection, also outside the writer lock (11:39: 19 turns and 48 writers queued
  behind it); usermerge floor and bounded pages unchanged.
- Compaction recomputes historical display identities on a pooled-reader snapshot before
  taking `SessionDB._lock`. Under the writer lock it folds from the display indexes and
  recomputes only new or changed rows, including user `display_metadata` edits not watched
  by the identity trigger. A missing reader or display index retains the full-scan fallback.
  Branch refreshes must preserve this off-lock path and its regression coverage.
- Generation stamping checkpoints only until its `application_id` is visible in the main DB file.
  A reader can block the first PASSIVE checkpoint and leave both the header and `state_meta` row
  WAL-only; later warm opens retry until the header is durable. The lock-safe header probe avoids
  cancelling process-wide SQLite locks. Fully checkpointed warm opens still skip the checkpoint
  (#494), and its deleted-sidecar descriptor rechecks remain unchanged.
- Each write's replaced/WAL-generation probe (stat of state.db and its sidecars, header pread)
  first runs lock-free; only a doubtful result runs the authoritative probe under `self._lock`
  (#105567's close race stays inside). 11:52: the lock holder sat in a kernel-stalled stat().
- Conversation boundaries clear the persisted `/yolo` off the loop: the async funnel
  (`_clear_conversation_scope_locked_async`) and `/branch` clear memory on the loop and await
  `_persist_boundary_yolo_clear` in a thread. Codex app-server hygiene records its failure
  cooldown and resets its streak through `asyncio.to_thread`. Found by arming the guard on every
  test event loop (2026-10-26).
- A full routing-index save (`replace_gateway_routing_entries`) writes only the difference,
  with unchanged full-replace semantics; it used to rewrite every row of the scope per update.
  The diff runs inside SQLite (two statements over one `json_each` parameter), never as rows
  fetched into Python under the writer lock: each row round trip re-takes the GIL, and with
  ~120 busy agent threads that turned a 10 ms diff of 2,753 rows into 3-17 s holds
  (2026-10-25: 58 holds, 365 s in 2 h; callback CPU ~0.4 s). Keep new writer callbacks
  O(1) in Python round trips where the row count grows with history or sessions.
- The turn-liveness watchdog does not count time the turn's execution thread spends inside a
  SessionDB write (writer-lock queue, busy retries, commit) as no progress; a finished write
  counts as activity. Nothing else (lease refresh included) feeds the activity clock.

## Active replay access path

Source: `hermes_state_messages.py::_fetch_conversation_rows`; proof:
`tests/hermes_state/test_active_conversation_replay.py`. On fork base
`54f27baa0695c9b0f8bf57ba0184944130afb8ba`, the exact public replay projection chose
`idx_messages_session_id`, filtering 332,311 archived rows to restore 173 live rows.
Active-only `ORDER BY +id` lets SQLite use the existing `(session_id, active, timestamp)`
index and sort only live rows, preserving integer id order without a forced-index dependency.
Nine paired warm reads in one read-only snapshot: full SQL median 165.91 → 0.73 ms;
public replay with alternation repair 167.32 → 3.71 ms. A second archive-heavy session and
2k/20k disposable generations preserve every selected column and decoded message in order.
The regression is red on the base (10x archives cost 10x VM steps) and green after the fix.
Display/audit predicates and sorts, lineage, repair, prompt sidecars, and compaction tail
watermarks are unchanged. Upstream `835c3dd038d903182a66ecc45e34bf5e0de65f00` has the same
query; related PR/issue searches found no matching access-path fix. Upstream contribution
is deferred to the fork delivery owner. [PR #516](https://github.com/0xble/hermes-agent/pull/516)
is the publication and integration record for this fork correction. Runtime activation is
recorded separately by the native updater's `~/.hermes/logs/update_receipts/` and the
`~/.hermes/current` release pointer, never inferred from this source file or PR publication.
Revert this query change to roll back; retire it when an upstream release supplies
archive-bounded id-ordered replay.

## Crash-recovery integrity scan

After an unclean prior life, `gateway/lifecycle_ledger.py::record_startup` logs
that WARNING and sets `prior_unclean_exit` / `prior_suspected_oom` on the new
sentinel. In WAL mode it claims that sentinel before the asynchronous scan; in
DELETE/unreadable-header mode it completes the synchronous scan before claiming
it. Where `PRAGMA quick_check(1)` runs depends on the on-disk journal mode, read
lock-free from header bytes 18/19:

- **WAL (default):** `record_startup` first appends a
  `gateway.previous_unclean_exit` record with `state_db_integrity: "pending"`, then
  a context-bound daemon worker runs the scan. The worker logs ERROR on corruption
  and appends the completed verdict to exit-diag. It never renews a startup-watchdog
  lease (unrelated startup must prove its own progress). Process exit can
  interrupt it; no shutdown wait or timeout increase is introduced.
  If exit interrupts the worker or thread startup fails, the pending record keeps
  the prior-life evidence after the sentinel has been claimed. It is not an
  integrity verdict; the later record for the same prior life carries that result.
- **Anything else (DELETE / unreadable header):** the scan runs synchronously
  inside `record_startup`, before this process claims the new sentinel and before
  `runner.start()` opens any SessionDB writer, under the renewed
  `state_db_unclean_integrity_check` lease
  (#115542), exactly as before this patch. Keeping the prior life as sentinel
  owner until the scan ends means a watchdog exit during a stalled scan cannot
  mark that prior life clean and suppress the next boot's check.
  A rollback-journal reader holds SHARED
  for the whole scan, so a background scan would fail every writer with
  `database is locked` (review repro: `SessionDB.create_session` failed after
  60.1 s). Stores on `database.journal_mode: delete` (virtiofs/NFS/SMB) or forced
  to DELETE by the WAL-reset gate keep the slow crash-restart; that is the price
  of losing no writes.

Concurrency limits of the WAL background scan, accepted rather than bounded:

- Its read transaction pins one snapshot for the whole scan (134 s on the 28 GB
  store; about 4200 s on a ~37 GB store in the worst observed case). Writers
  proceed, but no checkpoint can reset or truncate the WAL past that snapshot for
  the full scan duration, so `state.db-wal` can grow with every write until the
  scan ends; the next checkpoint after it reclaims as usual.
- A `wal_checkpoint(TRUNCATE)` in that window waits out its connection's busy
  timeout, then returns `busy=1` without resetting (no exception). The
  post-VACUUM TRUNCATE in `hermes_state_maintenance.py::vacuum` therefore leaves
  the WAL at its high-water mark until a later checkpoint. `VACUUM` itself
  succeeds beside the pinned reader (probed on SQLite 3.53.1: TRUNCATE `(1, 2423,
  213)` after 1.1 s, VACUUM ok, TRUNCATE `(0, 0, 0)` once the reader ended). Requested compaction (`_run_requested_state_db_compaction`)
  runs before `record_startup`, so it never overlaps the scan; the housekeeping
  auto-VACUUM waits for its first watch window and its own throttles.
- The checker connection is opened through the tracked SQLite helper and stays
  registered until the scan closes it. This in-process overlap is required:
  `SessionDB` startup's header probe must see the live path and skip raw
  `open()`/`close()`, because an untracked descriptor close can cancel this
  process's POSIX locks. The pending and completed records intentionally retain
  the same `gateway.previous_unclean_exit` tag; pair them by
  `(prior_pid, prior_started_at)`, with `state_db_integrity: "pending"` versus
  the completed verdict distinguishing the two stages.
- Other processes (cron workers, CLI) can write throughout; only this
  process's own readiness was ever gated.

The 2026-10-12 production SIGKILL recovery respawned in 0.5 s, listened on its
control socket at 00:22:05.461 PDT, then stalled until 00:24:19.534 before reporting
the prior unclean life. Telegram polling was healthy at 00:24:21.3. A healthy
28 GB state.db returned `ok`; its synchronous integrity scan cost 134 s.
Upstream main `590af66a58c2b3eb3c5edd1dcfefa09c625ae18a` still runs this check
synchronously. Upstream `2a8c3b313c72` extended the watchdog lease while it ran;
this patch preserves corruption detection without putting the scan on the
readiness path or masking startup stalls with background progress.

Proof: `tests/gateway/test_lifecycle_state_db_check.py` builds real WAL and
DELETE stores. WAL: an event-held checker proves startup returns with the
sentinel claimed, a pending diagnostic already written before the scan begins,
and no lease taken, then the persisted verdict and corruption
ERROR. DELETE: the held checker proves `record_startup` does not return until
the scan ends, after which a zero-timeout writer commits; the scan renews the
startup lease. Both modes persist healthy/corrupt verdicts; clean boots never
scan. `tests/gateway/test_lifecycle_ledger.py` (unchanged) preserves unclean/OOM
flags and the exit-diag schema. Retire this split only when upstream provides
equivalent readiness and forensics behavior.

## 2026-10-10 and 2026-10-11 incidents

The gateway exited with code 75 four times on 2026-10-10 (19:07, 19:31, 19:33 and 19:44 PDT)
and again on 2026-10-11 at 09:22 PDT. Each time the liveness watchdog killed it while the loop
thread waited on SessionDB or session-store locks. The 09:22 stack was `_handle_message` →
`_hm_evict_reaped_agent` → `SessionStore.peek_session_id`. Every boot re-created the herd:
about 25 auto-resumes, goal continuations and about 20 cron catch-ups, all on a saturated
disk. Upstream has no matching issue or PR (searched 2026-10-10).

## Patch

**Patch identity:** `gateway-loop-statedb-guard`. Source surfaces: `gateway/run_inbound.py`
(reaped-session probe), `gateway/run_startup.py` (resume admission), `agent/turn_facade_lease.py` (post-expiry lock
tolerance), `agent/turn_liveness.py`, `hermes_state.py` (checkpoint connection, write marker),
`hermes_state_gateway.py` (routing diff), `hermes_state_health.py`,
`hermes_state_messages.py` (off-lock historical display fold),
`cron/scheduler.py` and `hermes_cli/config_defaults.py` (cron bound), and the 2026-10-25 sweep
in `gateway/run_turn.py`, `gateway/run_config_loaders.py`, `gateway/slash_commands_status.py`,
`gateway/delivery.py` and the Slack adapter. The Telegram retire, topic-session and adapter
sites were retired with the [Telegram stack](telegram-stack-retirement.md). Proof surface:
`tests/gateway/test_startup_resume_admission.py`,
`test_reaped_probe_runs_off_the_event_loop` in `tests/gateway/test_reaped_eviction_interrupts_run.py`,
`test_max_parallel_is_bounded_unless_explicitly_unbounded` in `tests/cron/test_scheduler.py`, and
the lock-tolerance and delayed-renewal cases in `tests/agent/test_turn_facade_lease.py`,
`test_writers_proceed_while_a_slow_checkpoint_runs` and `test_writers_proceed_while_a_slow_fts_merge_runs`
in `tests/hermes_state/test_wal_checkpoint_strategy.py`,
`tests/hermes_state/test_generation_checkpoint_durability.py` (real reader-blocked first checkpoint;
main-file-only snapshot proves header and row durability after a warm open),
`test_warm_writable_open_skips_generation_checkpoint` in `tests/hermes_state/test_hermes_state.py`,
`tests/hermes_state/test_gateway_routing_replace_diff.py`, `tests/hermes_state/test_db_identity_probe_off_lock.py`,
`tests/hermes_state/test_archive_compact_off_writer_lock.py` (event-gated writer availability,
changed rows and user metadata, fold equivalence and non-WAL fallback), and
`test_waiting_on_a_contended_state_db_writer_is_not_a_stall` in `tests/agent/test_turn_liveness_watchdog.py`.

Further proof surface: `tests/hermes_state/test_loop_guard.py`, `tests/hermes_state/test_holder_scan_bounded.py`,
`test_goal_family_commands_keep_loop_live` in `tests/gateway/test_turn_state_db_off_loop.py`, and the
boot-delay case in `tests/hermes_cli/test_worktree_pushed_tier.py`, and
`tests/gateway/test_loop_thread_statedb_sites.py`, `tests/gateway/test_loop_thread_statedb_sweep.py`.
Known remaining loop site: adapter-reconnect
recovery (`_recover_spool_after_reconnect`) stays synchronous on purpose (e0ba43707ba1): its
candidate snapshot, spool claim and key resolution must finish before the per-session reconnect
gate is installed, with no await in between, or fresh inbound can overtake a claimed follow-up.

**Retire** an offload when upstream moves the same call off the loop. Retire the herd bounds when
upstream bounds restart replay and cron catch-up itself.
