# Profile authority pending store open

## Required behavior

A restored or hot-served authority joins the single open already in progress for
its profile's store. Opening in progress is not an unusable database: the
optional-read `None` sentinel must not become a terminal `profile_parked` verdict
or poison the runner wrapper cache with `SessionStore SQLite handle unavailable`.
Authority construction resolves the scoped handle off-loop. Ordinary fallback
reads remain nonblocking; failed-open backoff, real corruption/permission park
reasons, registry generation fences and one-writer ownership remain unchanged.
No new retries or enlarged deadlines are used.

## Evidence and ownership

Upstream PR #140908 / commit cedcf7a0b6 made runner wrappers borrow the SessionStore
handle. The shared cache still returns `None` while that handle is opening. After
snapshot unserve closes the registry generation, a worker opening the satellite
store can therefore overlap the rejoin. The wrapper treats the transient sentinel
as a failed open and caches it; the authority parks before the healthy opener
finishes. Hosted gate runs 41779489001 (#1063), 41785139245 (#929), and
41789074329 (#1074), attempt 1 shard 4, show that same failure at rejoin, but do
not expose which background reader started the open. The deterministic regression
holds the real registry reacquire after handoff and tests overlap in each cache.
Both variants fail on the unmodified product, then pass with the required-open
join. The live snapshot suite still tests all three root/secondary/missing cases.

Upstream PR #126430 is the related prior repair-after-failure proposal, not a
fix for in-flight borrowing. Upstream main 71736c9f32597bbca9d89cede5aa136e55946b2f
was inspected; no upstream tracking PR was created for this fork-specific repair.

Patch identity: `profile-authority-pending-open`.

Contract tests:
- `tests/gateway/test_profile_authority_pending_open.py`
- `tests/gateway/test_session_db_recovery.py`
- `tests/gateway/test_session_db_required_open.py`
- `tests/gateway/test_session_db_handle_sharing.py`
- `tests/gateway/test_profile_authority_park_reason.py`
- `tests/hermes_cli/test_gateway_snapshot_restore_live.py`
- `tests/hermes_cli/test_gateway_snapshot_restore_guards.py`

Retire this unit when upstream required authority opens join the same in-flight
attempt without poisoning fallback caches, and these contracts pass unchanged.
