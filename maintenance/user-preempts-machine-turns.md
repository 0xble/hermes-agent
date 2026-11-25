# User messages preempt machine-origin turns on the lease wait

Fork patch identity: `user-preempts-machine-turns`.

## Required behavior

A user message that arrives while an internal (machine-origin) turn holds the
session's turn lease must not wait for that turn to finish. Internal turns include
watch and process notifications, delegation completions, and loop or goal wakes,
all delivered through `gateway/wake.py` with `internal=True`.

Such messages take the cold admission path and wait in
`SessionTurnLeaseRegistry.acquire` (`gateway/turn_lease.py`), so the adapter busy
path's preemption in `gateway/run_busy.py` never sees them. Their only
acknowledgement was the lease-wait notice ("Queued for the next turn"). On
2026-11-23 a watch notification held a Telegram topic for 517 s while 2 user
messages waited behind it.

The fix:

- Lease tokens carry their originating event, and `acquire()` takes an
  `on_contended` callback that runs once when the lease is already held.
- `_preempt_machine_holder` in `gateway/run_turn_prepare.py` soft-interrupts the
  holder's agent with `interrupt(None, tool_reason="gateway user turn preemption")`
  (a system-sourced interrupt that carries no user text, so the user's message runs
  once as the next lease owner), and sends the existing "Interrupting" busy ack,
  which suppresses the later queued notice.

Preemption happens only when all of these hold:

- the waiter is a genuine user turn (`_is_user_turn_event`, not agent-origin text)
- the holder's event is internal
- the busy mode is `queue` or `interrupt` (`steer` keeps waiting)
- the holder agent has no active subagents and no compression in flight
- the holder has a live agent (a pending sentinel or missing agent means wait)

- The session task is never cancelled. Soft preemption is cooperative: if the machine turn is inside a non-interruptible tool or step, it keeps the lease until that step yields. The user message remains FIFO behind it; the user has already seen the "Interrupting" acknowledgement rather than a second queued acknowledgement.

Contract tests: `tests/gateway/test_turn_lease_queue.py`.

## Interrupt attribution on early returns

The gateway suppresses a preempted machine turn's reply only when its result
carries `interrupt_source == "gateway_user_turn_preemption"`. Every interrupted
return that skips `finalize_turn()` must call `stamp_interrupt_result()` before
`clear_interrupt()` resets `_tool_interrupt_reason` and `_interrupt_source`,
including message-less system stops:

- `abort_turn_on_interrupt()` (API-error recovery)
- the turn-lease early return in `agent/turn_facade_lease.py`

Without the stamp, the preemption returns `interrupted=True` with no source, so
the gateway cannot tell it from a user stop. Covered by
`tests/agent/test_system_machine_preemption.py` and
`tests/agent/test_turn_facade_lease.py::test_message_less_system_interrupt_keeps_its_source`.

## Upstream

Fork-only. Upstream has no machine-turn preemption on the turn-lease wait.
