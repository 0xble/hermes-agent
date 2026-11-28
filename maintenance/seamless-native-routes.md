# Seamless native route mounting

Fork patch identity: `seamless-native-routes`.

## Contract

The busy native `/stop` route may read durable FIFO ownership and a cold-route
mount plan off the gateway loop because the session store can block on SQLite
locks. It must publish the resolved route and mutate `authority.sessions` on the
gateway loop, preserving ordering with admission, recovery and shutdown.

The read-only durable probe and mount-plan read are separate from the
event-loop-owned mount. A probe failure must not move publication off-loop or
silently skip the route recovery contract.

## Source surfaces

- `gateway/run_inbound.py`
- `gateway/session_route_recovery.py`
- `tests/gateway/test_native_admission_arrival_order.py`
- `tests/gateway/test_stop_route_mount_thread.py`

Contract tests: `tests/gateway/test_native_admission_arrival_order.py::test_stop_owner_session_probe_runs_off_gateway_loop`; `tests/gateway/test_native_admission_arrival_order.py::test_stop_reprobes_after_fence_when_first_owner_commits_during_probe`; `tests/gateway/test_stop_route_mount_thread.py::test_stop_route_mount_keeps_authority_mutation_on_gateway_loop`

## Maintenance decision

Keep blocking durable reads in the worker-thread probe and keep authority/session
mounting on the gateway loop. Retire this unit when the upstream route-recovery
implementation preserves both boundaries and the fork no longer carries the
patch.
