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

Retire a waiting event's spool only when the exact platform or recovery identity
is present in the transcript. Otherwise leave it for the existing startup
follow-up recovery path. Reuse its authorization, profile routing, media,
identity-based duplicate handling, and visible stale-input decisions.

Contention logs distinguish same-key generation overlap from different routing
keys. The lease's default bounded/fail-closed behavior remains available to
callers that do not supply a durable waiting owner. Cross-process turn leases
in `agent/turn_facade_lease.py` are unchanged.

## Proof And Retirement

`tests/gateway/test_turn_lease_queue.py` covers same-key and aliased-key dispatch,
FIFO across the deadline, cancellation, capacity pressure, failed spool writes,
media and id-less recovery, and spool retirement only after a durable input.
`tests/gateway/test_turn_lease.py` covers the real preparation/dispatch boundary,
including no transcript access or goal judgment while waiting.

Retire this patch when an accepted upstream release preserves admitted inputs
across the same generation-overlap boundary with equivalent serialization and
restart-replay guarantees. No deployment follows merely from source publication.
