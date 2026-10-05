# Parked goal idle wake

## Required behavior

A `/goal` parked by the judge on a process, pid, or timer resumes on its own once the wait
ends. It must not depend on a later turn. The post-turn judge was the only re-check, and it
needs a turn, normally the process's completion notice. Three cases never produced that turn:
a gateway restart killed the process (`kill_all`) and its in-memory completion notice died with
the old process; the process ran without `notify_on_complete`; or a timed wait elapsed in an
idle chat. Each one left the goal parked until an unrelated user message arrived. One observed
case stayed parked for 14.5 hours.

- `GoalManager.lifted_barrier_prompt()` is the single shared pure check and appends one factual line
  about an awaited process. Live pid/session barriers are refreshed separately with escalating
  backoff after 30 minutes, using `barrier_recheck_at` rather than `waiting_until` so target
  presentation remains unchanged. The initial deadline is derived from `waiting_since` and is not
  persisted at park time, keeping CLI, gateway, and TUI state identical. The first age notice is
  deduped through `last_age_notice_key`; parked notices remain in `last_wait_notice_key`, and
  continuation notices use `last_continuation_notice_key`. A still-live target pauses with a named
  blocker after six hours. Liveness comes from the process registry or, after a restart, the durable
  `logs/process-results` receipt: a target may be killed by a restart or shutdown, killed explicitly,
  finished with its exit code, or no longer tracked (outcome unknown).
- Surfaces clear the barrier only after the continuation was admitted, through
  `clear_lifted_wait(waiting_since)`. A failed or refused injection is retried on the next scan,
  and a resumed turn that has already re-parked keeps its newer barrier.
- A tracked `notify_on_complete` process that exited in this process defers for
  `_COMPLETION_NOTICE_GRACE_S` so its own completion turn re-judges first.
- Gateway: `_loop_wakeup_watcher` (15 s, all served profiles) also scans parked goals through
  `list_parked_goals`, gated per profile by `profile_has_parked_goal`. Delivery uses the persisted
  routing entry and `_restored_source`. The scan defers while a turn runs, while the adapter
  guard is held, while messages are queued, while **fresh** restart auto-resume (`resume_pending`)
  owns the chat (see [restart-parked goal wake](goal-restart-parked-wake.md)), and for suspended or
  unroutable sessions. One ticker owns both `/loop` and goal idle
  injection, so any future overlapping-gateway admission fence has one owner to transfer.
- TUI/Desktop/dashboard: the session-owner notification poller resumes the goal like `/loop` and
  `/heartbeat` ticks and leaves gateway-routed conversations to the gateway.
- Classic CLI keeps its idle hook and now uses the same shared check and note.

## Non-goals

Restart cleanup still kills background tool processes. Persisting completion notices for
processes killed at shutdown belongs to seamless-restart Phase 1's durable result delivery, not
this patch. When that lands, the note can report the delivered result, and this wake remains
the fallback for timed waits and processes without notification.

## Provenance and upstream

Fork patch identity: `goal-parked-idle-wake`. Builds on the CLI idle hook from 1f3912d and the
barrier cap from f8b87f5. Upstream `main` has the same gap: its gateway and TUI have no idle
re-check. No upstream issue or pull request was found for "goal parked gateway", "goal wait
barrier", or "goal parked forever" as of 2026-09-26.

## Verification

- Focused verification uses exact one-file commands:
  - `tests/hermes_cli/test_goal_parked_idle_wake.py`
  - `tests/gateway/test_goal_continuation_drain.py`
  - `tests/gateway/test_goal_parked_idle_wake.py`
  - `tests/tui_gateway/test_goal_parked_idle_wake.py`
  - `tests/hermes_cli/test_goals.py`
  - `tests/hermes_cli/test_goal_external_wait_backoff.py`
  - `tests/hermes_cli/test_goal_dispatch.py`
  plus the related goal and session-control modules. The repository-wide
  `scripts/run_tests.sh` wrapper is intentionally not used here.

## Retirement and rollback

Retire when upstream ships an equivalent idle re-check across gateway and TUI that passes these
tests. Roll back by reverting this patch's commit. No schema, config, or state migration is
introduced, and parked goals keep their barriers.
