# Gateway loop state.db guard

Load this unit when gateway async code reads or writes `state.db`, the session store, or a
goal/loop/heartbeat manager; when changing `SessionDB` locking or open; or when changing what
a gateway restart starts at once (restart auto-resume, cron catch-up, boot-time maintenance).

## Required behavior

- The gateway event-loop thread never waits on `state.db`. Loop code reaches the SessionDB
  writer lock, the session-store lock (held across a turn marker or routing save), a SessionDB
  open, and GoalManager/LoopManager/HeartbeatManager load or save through `asyncio.to_thread`
  or `_run_in_executor_with_context`. [Seamless restart](seamless-restart.md) (PR 475) owns the
  per-turn, notification and topic-recovery hops; this unit adds the `/subgoal`, `/heartbeat`
  and `/loop` command hops and the guard that keeps new sites from slipping in.
- `hermes_loop_guard` enforces it. The gateway registers its loop thread when it binds the
  loop at startup and clears it when `stop()` disarms the liveness guards. The SessionDB writer
  lock and the session-store lock are `LoopGuardedLock`s, and `SessionDB.__init__` checks too.
  On the registered thread a blocking acquisition raises `LoopThreadBlockingError` under pytest
  and logs one WARNING per call site in production. Production never raises.
- A restart admits resumed turns `_STARTUP_RESUME_CONCURRENCY` (4) at a time. Each slot is held
  until the turn ends or `_STARTUP_RESUME_OPENING_S` (15 s) passes. `cron.max_parallel_jobs`
  defaults to 4 (`0` = unbounded), so the first tick's catch-up is bounded too. The cron-tick
  worktree prune waits 30 minutes after process start.
- The pre-open deleted-WAL holder scan (host-wide libproc on macOS) reuses a clean verdict for
  10 s while the sidecars keep their identity. It runs once per path at a time, and fails open
  after 2 s (a cold scan took 77 s across 1,100 processes on the loaded host). The storage-health latch skips path resolution while nothing is latched.

## 2026-10-10 incident

The gateway exited with code 75 four times (19:07, 19:31, 19:33, 19:44 PDT) on release
`808abcb81df5`. Each loop-liveness kill had the main thread waiting on SessionDB or
session-store locks while about 30 resumed turns contended the writer (PR 475 moved those
sites). Every boot re-created the herd: about 25 auto-resumes, goal continuations and about
20 cron catch-ups, on a host whose disk was saturated. Stuck test processes on the same host
sat in `proc_pidfdinfo` inside the pre-open holder scan, and a cold scan took 77 s. Upstream
has no matching issue or PR (searched 2026-10-10).

## Patch

**Patch identity:** `gateway-loop-statedb-guard`. Source surfaces: `hermes_loop_guard.py`,
`hermes_state.py` (writer lock, open check), `gateway/session.py` (store lock),
`gateway/run_startup.py` and `gateway/run_shutdown.py` (arm/disarm, resume admission),
`gateway/slash_commands_goals.py`, `hermes_state_dbfile.py` (bounded holder scan),
`hermes_state_health.py`, `cron/scheduler.py` and `hermes_cli/config_defaults.py` (cron bounds).
Proof surface: `tests/hermes_state/test_loop_guard.py`, `tests/hermes_state/test_holder_scan_bounded.py`,
`tests/gateway/test_startup_resume_admission.py`, `test_goal_family_commands_keep_loop_live` in
`tests/gateway/test_turn_state_db_off_loop.py`, and the cron default and boot-delay cases in
`tests/cron/test_scheduler.py` and `tests/hermes_cli/test_worktree_pushed_tier.py`.

**Known remaining loop sites** (logged by the guard in production): sync helpers reached
during startup or reconnect (`_normalize_source_for_session_key`) and first-access `_session_db`
opens for a profile scope not yet warmed.

**Retire** each offload when upstream moves the same call off the loop. Keep the guard while
any gateway code can reach state.db synchronously.
