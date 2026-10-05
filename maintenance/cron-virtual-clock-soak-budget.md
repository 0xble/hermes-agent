# Cron virtual-clock soak budget

## Required behavior

The Linux C13 soak keeps its complete scenario matrix, `HOLD_STEPS`, and
`REPLICA_ROUNDS` while remaining below the portable test runner's default
per-file budget. The test must exercise the real in-process ticker, shared-store
child ticker, fire-claim contention, heartbeat lease refresh, crash recovery,
delivery ledger, and independent schedule oracle.

## Provenance and disposition

Fork patch identity: `cron-virtual-clock-soak-budget`.

The baseline's `SchedulerHost.wait_idle()` polled a condition variable through
the generic filesystem-oriented `wait_until()` helper after every released
virtual tick. On the 2-CPU Linux probe, the profiler attributed 20.476 seconds
of the 56.854-second run to 1,855 in-process ticker waits. The host now waits
on the existing `_StepGate.cv` and receives an explicit exception notification
from the ticker thread. No scheduler behavior, scenario, hold step, replica
round, or assertion changed.

The proof surface is `tests/e2e/core/delivery/test_cron_virtual_clock_soak.py`
in `python:3.14-bookworm` with two CPUs and a tmpfs `/tmp`. Baseline runs were
58.42s, 55.87s, and 55.55s; candidate runs were 53.69s, 57.87s, and 52.77s.
All runs reported `5 passed, 1 xfailed`; the C13 stats and replica assertions
were unchanged. The xfail is the pre-existing, separately owned open schedule
oracle gap and is not introduced by this patch.

## Retirement

Retire this patch when the selected upstream release provides an equivalent
event-driven stepping harness and the full soak remains within the repository's
per-file budget without this fork adaptation.
