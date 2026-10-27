# Background Turn Admission

Load this unit when changing how goal continuations, barrier wakes, `/loop` ticks, delegation
retries, process or delegation completions, or restart auto-resumes start turns, or when touching
`gateway/replay_scheduler.py`, `gateway/run_background_admission.py` or
`gateway/wake.py::admit_internal_event`.

## Required Behavior

- Typed input is never gated. Only turns nobody typed ask the gate. Promotion and the load use
  `turn_origin_for_event`, so relays never count as typed input.
- At most `gateway.background_turn_concurrency` (default 8) turns nobody typed run at once.
  The load is measured from live runner state (sessions whose running turn's
  `turn_origin_for_event` is not `user`), so relays, heartbeats and chained goal
  continuations count against the cap even though they do not pass the gate, and a turn that
  ends, crashes or is stopped frees its slot with no release bookkeeping.
- Gated: restart auto-resumes (`_schedule_resume_pending_sessions`) and every
  `admit_internal_event` wake whose origin is `goal_loop` or `process` (goal continuations,
  lifted barriers, session-control continuations, loop ticks, delegation retry notices, process,
  watch-pattern and async delegation completions). Cron deliveries and reviews, opening briefs,
  plugin injections and relays are not gated.
- Resumes wait in the replay scheduler's priority queue (FIFO within lanes, behind restored typed
  input). Wakes never wait: with no room, `admit_internal_event` raises `WakeNotAccepted` before
  the adapter sees the event and the caller keeps its durable claim (lifted barrier, ledger row,
  requeued completion) and retries on its own cadence. Waiting in line was tried first and
  rejected: claims with 300 s leases would outlive the wait, serial watchers would stall, and a
  turn whose post-turn hooks drain another session's watch event would deadlock while it still
  counts against the cap.
- A wake for a session that already has a live turn or adapter guard joins that turn's FIFO
  and needs no slot.
- Each reservation is a token owned by one caller. `admit_internal_event` takes one after the
  currency and strict-pin checks and releases it when the adapter refuses the event. Callers
  that take a durable claim first reserve before claiming and stamp the token on the event
  (`BACKGROUND_RESERVATION_ATTR`), and admission honors a stamped token unconditionally: a
  `/loop` tick reserves before `fire_tick` (no room keeps the tick due) and a lifted goal barrier
  reserves before its stale marker is cleared. Both release the token on every non-admitting exit.
- A reservation counts its session until a turn claims it (`conversation.last_turn_started_at`)
  or 30 s pass, so turns that have not registered yet cannot overshoot the cap.
- Only queued resumes are spaced (`BACKGROUND_TURN_SPACING_S`, 1 s between grants). Refused
  wakes already retry on their own cadence, so spacing them would only delay them while slots
  are free.
- A person waiting on a session promotes that session's queued resume past the gate: any typed
  message reaching `_hm_handle_running_session_message`, and a recovered typed follow-up whose
  resume is still queued (not spooled internal events or declined follow-ups).
- Once the gateway drains, queued resumes are dropped, never started, and release their
  preclaims (markers stay durable for the next boot), so the drain does not wait on them.
- `gateway.restart_replay_concurrency` keeps bounding replay dispatch (turn openings); it is a
  separate knob from this turn cap.

## Source Surfaces

`gateway/replay_scheduler.py` (`BackgroundTurnGate`, gated items, `promote`, drain drop),
`gateway/run_background_admission.py` (scheduler ownership, `_admit_background_turn`,
`_reserve_background_turn`, `_background_turn_room`, `_background_turn_keys`, `_promote_background_admission`),
`gateway/wake.py` (`admit_internal_event`), `gateway/run_goals.py` (loop tick reservation, goal
wakeup room check), `gateway/run_restart_recovery.py` (gated resumes, recovered follow-up
promotion), `gateway/run_inbound.py` (preclaim promotion), `gateway/config.py` and
`gateway/config_loader.py` (`background_turn_concurrency`), `hermes_cli/config_defaults.py`.

## Known Trade-Offs

- Wakes retry on their own cadence (watchers every 2 s, goal and loop scans every 15 s), so
  order among waiting wakes is not FIFO; resumes poll the gate every second and usually win.
- A goal whose continuations chain inside one session holds its slot until it parks, finishes
  or reaches its turn limit. Round-robin between goals is later, if needed.
- Queued resumes keep their preclaim sentinels, so `active_agents` in `gateway_state.json`
  counts them while they wait, and so does `max_concurrent_sessions` when it is set.
- The load counts every running turn nobody typed (relays, cron deliveries, heartbeats and
  chained continuations too), but only the gated kinds above are ever refused.

## Verification

`scripts/run_tests.sh tests/gateway/test_background_turn_admission.py tests/gateway/test_replay_scheduler.py`

## Provenance

- Fork patch identity: `background-turn-admission`.
- Evidence (2026-10-25, Personal gateway at `787a4115`): after a liveness-watchdog exit, about
  120 turns nobody typed started within 12 minutes (53 restart resumes, 25 goal continuations,
  22 lifted barriers, 13 delegation retries and more), `active_agents` held at 119-130 with load
  average about 40, and a typed "Hello?" took 224 s end to end for one 4.9 s model call. State.db
  write transactions held the lock 3-16 s (`archive_and_compact` alone: 589 slow transactions,
  5,557 s of lock time that day).
- Lease refresh and activity heartbeats were measured and left alone: each is one single-row
  UPDATE per agent per 60 s, never appeared among slow transactions, and blocked only behind
  the multi-second ones.
- Upstream has no gateway-wide cap on autonomous turns (searched 2026-10-25). Retire this when
  upstream admits autonomous turns behind a shared limit that leaves typed input ungated.
- Rollback: revert the commit carrying `Fork-Patch: background-turn-admission`. Restart replay
  returns to opening-only admission and wakes start immediately.
