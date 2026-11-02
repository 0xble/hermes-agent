# Runtime Epoch Writer Ownership

Fork patch identity: `runtime-epoch-ownership`.

## Intent And Provenance

On 2026-10-30 the running gateway's epoch moved from 35 to 36, and its saves
failed with `stale_epoch` from 6:25 to 6:28 PM PT until restart. The most likely
writer was an ad hoc probe importing a gateway test helper outside pytest.
That process attribution is inferred from the helper, fixture-row provenance,
and timing, not directly observed. The unchecked epoch writer was independently
reproduced on isolated temporary stores.

This fork enforces the writer's existing ownership contract regardless of test
markers or database location. The upstream unchecked increment cannot replace
this guard merely because test imports or PID-record checks become stricter.

## Adoption Rules

Preserve `gateway/runtime_ownership.py::assert_runtime_epoch_owner` around the
complete `hermes_state_runtime.py::begin_runtime_epoch` write. Authority comes
from the calling process's exact resolved-home reservation or the current
thread's active exclusive-maintenance reservation. Reservation release must not
race the write. A PID record, endpoint ticket, host lock, or another process's
slot lock is not sufficient.

Keep legitimate temporary-store test callers on `tests/fakes/runtime_owner.py`.
Those helpers acquire actual reservations only in the test-isolation context.
Unmarked importers receive no automatic ownership. The epoch writer itself has
no temporary-path or pytest exemption.

Offline dashboard initialization uses its existing maintenance reservation.
Backup restore stages a floor in a private image before publication, not through
the runtime epoch writer. Do not turn that staging step into an owner startup.

## Regression And Retirement

Contract tests: `tests/hermes_state/test_runtime_epoch_ownership.py`

The ownership regression is `tests/hermes_state/test_runtime_epoch_ownership.py`.
Its unreserved custom-home cases reproduce both absent and existing epoch rows
with test markers cleared. The same file exercises gateway worker-thread
ownership, thread-local maintenance, offline dashboard initialization, and the
unchanged staged restore floor.

When epoch writing, gateway ownership, offline maintenance, or authority test
fixtures change, run the regression and affected caller files with
`scripts/run_tests.sh`, plus the repository lint and footgun checks. All stores
must be temporary. No live-home probe is an acceptable regression.

Retire this divergence only when upstream enforces equivalent exact-home,
calling-process ownership at the epoch writer and the regression passes without
the fork guard. Retain temporary-home reservations in tests unless the upstream
fixture contract supplies them.

Main's foreign-owner and zero-cut successor checks remain additional refusal
conditions, not permission to skip this reservation. Keep new test fixtures on
the same real temporary-home helper, and retain `HERMES_TEST_ISOLATION` across
peer launch allowlists so asynchronous authority startup can reserve its home.
