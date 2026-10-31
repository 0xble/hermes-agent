# Runtime Epoch Owner Fence

Load when changing `begin_runtime_epoch`, `initialize_session_authority`, profile ownership reservations, the `tests` package import, or the runtime status fields readiness reads.

Patch identity: `runtime-epoch-owner-fence`.

## Required Behavior

- `begin_runtime_epoch` refuses with `OwnershipConflict` while another process holds the store home's `gateway.lock` (either slot). A reservation held anywhere in this process (`process_ownership`, an `exclusive_maintenance` reservation on any thread, an embedded `ProfileOwnership`) is never foreign, because a flock probe from the holder's own process would read its reservation as held. A home nobody holds stays open: the offline dashboard edit and temporary test homes begin as before, and the next owner's own begin supersedes such an epoch.
- Importing the `tests` package exports `HERMES_TEST_ISOLATION` (setdefault). A direct `python -` probe that reuses a test helper therefore arms the live-DB guard and fails at its first production `SessionDB()`. Under pytest the conftest replaces the value with its sandbox home.
- `initialize_session_authority` names the store-open failure (the runner's handle-cache error) instead of failing on `None.db_path`.
- The gateway housekeeping chore `Runtime epoch watch` compares each live authority's epoch with the store's. A move while this process still holds the profile logs CRITICAL once per taken epoch (naming the writer's instance id) and publishes `runtime_epoch: {"status": "lost", "held": N, "store": M}`. Readiness then reports `runtime_epoch: unavailable`. It never re-claims the epoch: the writer may still be running, and the recovery a begin runs would mark this owner's running turns `unknown`.

## Incident

On 2026-10-30 at 6:25 PM PT a delegated review agent ran a `python -` probe from a fork worktree. It imported `_busy_owner` from `tests/gateway/test_busy_burst_serial_reader.py`, whose `initialize_session_authority(instance_id='owner')` began epoch 36 on the live `~/.hermes/state.db` while the gateway held 35. The probe inherited the gateway shell's `HERMES_HOME`, ran no conftest and had no pytest ancestor, so the live-DB guard stayed disarmed. Its startup recovery also turned the gateway's started admissions `unknown`. Every later settlement failed `stale_epoch` until a manual restart. AGENTS.md already forbade such probes, so this patch enforces the rule in code.

## Verification and Retirement

`tests/hermes_state/test_direct_probe_isolation.py` runs the probe shape in a child with a temporary `HOME` and pytest ancestry pinned off. `tests/hermes_state/test_runtime_epoch_foreign_owner.py` holds the lock from a second process and covers in-process and maintenance reservations. `tests/gateway/test_runtime_epoch_watch.py` covers the once-only CRITICAL, the published status, released ownership and readiness. Each of the first two fails on the base revision, and the watch suite does not import there.

Retire when upstream fences epoch begins on profile ownership and reports a lost epoch, and these regressions pass without the local implementation. Reverting changes no schema or stored data.
