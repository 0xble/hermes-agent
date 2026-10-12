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
  through `_run_in_executor_with_context`.
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
- Generation stamping checkpoints only until its `application_id` is visible in the main DB file.
  A reader can block the first PASSIVE checkpoint and leave both the header and `state_meta` row
  WAL-only; later warm opens retry until the header is durable. The lock-safe header probe avoids
  cancelling process-wide SQLite locks. Fully checkpointed warm opens still skip the checkpoint
  (#494), and its deleted-sidecar descriptor rechecks remain unchanged.
- Each write's replaced/WAL-generation probe (stat of state.db and its sidecars, header pread)
  first runs lock-free; only a doubtful result runs the authoritative probe under `self._lock`
  (#105567's close race stays inside). 11:52: the lock holder sat in a kernel-stalled stat().
- A full routing-index save (`replace_gateway_routing_entries`) writes only the difference,
  with unchanged full-replace semantics; it used to rewrite every row of the scope per update.
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
query; related PR/issue searches found no matching access-path fix. Revert this query
change to roll back; retire it when an upstream release supplies archive-bounded
id-ordered replay.

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
`cron/scheduler.py` and `hermes_cli/config_defaults.py` (cron bound). Proof surface:
`tests/gateway/test_startup_resume_admission.py`,
`test_reaped_probe_runs_off_the_event_loop` in `tests/gateway/test_reaped_eviction_interrupts_run.py`,
`test_max_parallel_is_bounded_unless_explicitly_unbounded` in `tests/cron/test_scheduler.py`, and
the lock-tolerance and delayed-renewal cases in `tests/agent/test_turn_facade_lease.py`,
`test_writers_proceed_while_a_slow_checkpoint_runs` and `test_writers_proceed_while_a_slow_fts_merge_runs`
in `tests/hermes_state/test_wal_checkpoint_strategy.py`,
`tests/hermes_state/test_generation_checkpoint_durability.py` (real reader-blocked first checkpoint;
main-file-only snapshot proves header and row durability after a warm open),
`test_warm_writable_open_skips_generation_checkpoint` in `tests/hermes_state/test_hermes_state.py`,
`tests/hermes_state/test_gateway_routing_replace_diff.py`, `tests/hermes_state/test_db_identity_probe_off_lock.py`, and
`test_waiting_on_a_contended_state_db_writer_is_not_a_stall` in `tests/agent/test_turn_liveness_watchdog.py`.

Further proof surface: `tests/hermes_state/test_loop_guard.py`, `tests/hermes_state/test_holder_scan_bounded.py`,
`test_goal_family_commands_keep_loop_live` in `tests/gateway/test_turn_state_db_off_loop.py`, and the
boot-delay case in `tests/hermes_cli/test_worktree_pushed_tier.py`. Known remaining loop sites (logged
by the guard in production): startup/reconnect sync helpers (`_normalize_source_for_session_key`) and
first-access `_session_db` opens for an unwarmed profile scope.

**Retire** an offload when upstream moves the same call off the loop. Retire the herd bounds when
upstream bounds restart replay and cron catch-up itself.
