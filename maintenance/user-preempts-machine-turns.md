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

## Interrupt attribution follow-up

`abort_turn_on_interrupt()` is also a direct return path from API-error recovery;
it does not pass through `finalize_turn()`. It must stamp `interrupt_source`
while the pending interrupt state still exists, before `clear_interrupt()` resets
`_tool_interrupt_reason` and `_interrupt_source`. Without that ordering, a
message-less `gateway user turn preemption` returns `interrupted=True` but no
source, so the gateway cannot distinguish a system preemption from a user stop.
The regression is covered by
`tests/agent/test_system_machine_preemption.py::test_system_soft_interrupt_real_agent_records_source_without_followup`.

## Upstream

Fork-only. Upstream has no machine-turn preemption on the turn-lease wait.
