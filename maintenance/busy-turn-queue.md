# Busy Turn Queue

Patch identity: `busy-turn-queue`.

Load when changing gateway turn-lease admission, routing-key generation cleanup,
or pending-input recovery.

## Contract

The routing-key busy slot and per-session transcript lease have different
lifetimes. `_run_agent_cleanup_turn_tasks` can clear the former before the
outer `_handle_message` finally releases the latter. An admitted successor can
therefore have generation N+1 while the same routing key's generation N still
owns the transcript lease. Eviction and session aliases can also expose this
boundary. A routing generation is not permission to read an unlocked transcript.

After `HERMES_TURN_LEASE_TIMEOUT` (default 5 seconds), an inbound successor must
retain its original FIFO lock waiter, not reject the message or retry acquisition
at the tail. Spool the exact event with the existing private atomic pending-input
writer before extending its wait. Run spool writes and durability probes off the
asyncio thread. The existing routing-key sentinel continues to own admission,
so additional messages use the existing busy guards rather than spawn waiters
for the same key. Keep the existing queue capacity and idle-only lease eviction.

Cancellation must cancel and drain the lock acquire. A completed acquire racing
cancellation must release the lock if no turn token was published. A failed
spool write fails visibly and closed, never authorizing an unserialized turn.

The total wait is bounded by the owning profile's
`gateway.turn_lease_max_wait_seconds` (default 1800 seconds). After 30 seconds,
use the existing busy-ack channel for 1 queued notice, unless the event was
already acknowledged. Expiry and spool failures use the existing visible resend
reply and refund unrun loop ticks.

Retire a waiting event's spool after a durable user row, on deliberate input
rejection, or on user cancellation (`/stop`, `/new`, `/reset`). Retain undurable
input only for shutdown cancellation of a still-current generation or a crash.
User cancellation and deliberate rejection retire the input even after draining
begins. Settle an in-flight spool writer before cancellation finalizes replay
ownership. Recovered waiters reuse their
existing spool, and reconnect scans skip still-running routing keys. Startup
follow-up recovery retains its authorization, profile routing, media,
identity-based duplicate handling, and visible stale-input decisions.

Contention logs distinguish same-key generation overlap from different routing
keys. The lease's default bounded/fail-closed behavior remains available to
callers that do not supply a durable waiting owner. Cross-process turn leases
in `agent/turn_facade_lease.py` are unchanged.

Recovered follow-ups consumed in-band must publish their raw turn outcome on the
consumed event and its absorbed inputs. Intentional silence is a successful turn,
not missing execution. Admission and an unrelated predecessor result are not proof
of execution. Preserve the existing notices for accepted inputs that never run
and the declined or possible-duplicate recovery paths.

## Proof And Retirement

Contract tests: `tests/gateway/test_turn_lease_queue.py`,
  `tests/gateway/test_shutdown_followup_restart.py`,
  `tests/gateway/test_turn_lease.py::test_lease_wait_preserves_input_without_loading_transcript_or_goal_hook`,
  `tests/gateway/test_queued_followup_processing_hooks.py::test_recovered_in_band_followup_reports_outcome`

`tests/gateway/test_turn_lease_queue.py` covers same-key and aliased-key dispatch,
FIFO across the deadline, user cancellation versus shutdown, capacity pressure,
failed spool writes, bounded waits, busy-ack deduplication, media and id-less
recovery, recovery-spool reuse, reconnects, and spool retirement on deliberate drops.
`tests/gateway/test_turn_lease.py` covers the real preparation/dispatch boundary,
including no transcript access or goal judgment while waiting.
`tests/gateway/test_queued_followup_processing_hooks.py` drives recovered inputs
through busy admission and real recursive execution, including silent success,
absorbed inputs, and failed or interrupted turns.
`tests/gateway/test_shutdown_followup_restart.py` retains the non-run notices,
successor ownership, and declined or possible-duplicate guards.

Retire this patch when an accepted upstream release preserves admitted inputs
across the same generation-overlap boundary with equivalent serialization and
restart-replay guarantees. No deployment follows merely from source publication.
