# User messages preempt machine-origin turns on authority and lease contention

Fork patch identity: `user-preempts-machine-turns`.

## Required behavior

A typed user message that arrives while an internal (machine-origin) turn owns
an authority session in `busy_input_mode: queue` must not wait for that turn to
finish. Internal turns include watch and process notifications, delegation
completions, and loop or goal wakes, all delivered with `internal=True`.

There are two contention paths. Authority-owned sessions enter
`gateway/run_inbound.py::_hm_admit_to_fifo` and `_hm_busy_preempt` before durable
FIFO admission; cold turns on a resolved session wait in
`SessionTurnLeaseRegistry.acquire` (`gateway/turn_lease.py`). Both paths preserve
the user's message as one queued FIFO row, then cooperatively soft-interrupt the
machine turn with no user text. The adapter remains free to commit the row while
the interrupt and any pending-agent publication wait run in a tracked background
task.

Preemption happens only when all of these hold:

- the waiter is a genuine user turn (`_is_user_turn_event`, not agent-origin text)
- the holder's event is internal
- the busy mode is `queue` or `interrupt` (`steer` keeps waiting)
- the holder agent has no active subagents and no compression in flight
- the holder has a live agent (a pending sentinel or missing agent means wait)

Only typed `TEXT` in `queue` mode preempts; `steer` mode and media keep queue
semantics. Typed text behind a person's own turn stays silent, as on main.

For the authority path's pending-agent window, wait in the background on the
turn's `agent_published_event` or `turn_ended_event`, bounded by
`lease_wait_max_seconds()`, then re-check the same opener event, published agent,
and run generation. If the bound expires with no publication, the existing
queued acknowledgement is sent once and nothing is interrupted. This
keeps the wait a soft yield and prevents a late publication from interrupting a
successor turn; a pending-stop latch was not used because it is coupled to hard
stop attribution and would broaden the control path.

The session task is never cancelled. Soft preemption is cooperative: if the
machine turn is inside a non-interruptible tool or step, it keeps the lease until
that step yields; the user message remains FIFO behind it and receives the
existing "Interrupting" acknowledgement instead of a second queued notice.

Contract tests: `tests/gateway/test_authority_busy_preempt_parity.py::test_typed_text_behind_machine_turn_is_soft_preempted_once`, `tests/gateway/test_authority_busy_preempt_parity.py::test_typed_text_behind_machine_turn_with_active_subagents_stays_queued`, `tests/gateway/test_authority_busy_preempt_parity.py::test_agent_origin_text_behind_machine_turn_does_not_preempt`, `tests/gateway/test_authority_busy_preempt_parity.py::test_relay_event_behind_machine_turn_does_not_preempt`, `tests/gateway/test_authority_busy_preempt_parity.py::test_typed_text_behind_a_persons_own_turn_still_queues_silently`, `tests/gateway/test_turn_lease_queue.py::test_pending_internal_holder_publishes_then_is_preempted_once`, `tests/gateway/test_turn_lease_queue.py::test_pending_internal_holder_releases_without_publication_and_is_not_preempted`, `tests/gateway/test_authority_busy_preempt_parity.py::test_pending_machine_turn_never_published_gets_one_queued_ack_and_no_interrupt`, `tests/gateway/test_authority_busy_preempt_parity.py::test_steer_mode_and_media_keep_queue_semantics_behind_pending_machine_turn`, `tests/gateway/test_authority_busy_preempt_parity.py::test_pending_wait_ending_or_successor_turn_is_never_interrupted`, `tests/gateway/test_authority_busy_preempt_parity.py::test_adopt_agent_keeps_a_soft_preemption_but_still_drops_a_stale_stop`.

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
