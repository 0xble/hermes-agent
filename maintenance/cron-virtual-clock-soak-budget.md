# Cron virtual-clock soak budget

## Required behavior

The Linux C13 soak keeps its complete scenario matrix, `HOLD_STEPS`, and
`REPLICA_ROUNDS` while its harness waits for the in-process ticker by event
rather than by polling. The test must exercise the real in-process ticker,
shared-store child ticker, fire-claim contention, heartbeat lease refresh, crash
recovery, delivery ledger, and independent schedule oracle.

## Provenance and disposition

Fork patch identity: `cron-virtual-clock-soak-budget`.

The baseline's `SchedulerHost.wait_idle()` polled a condition variable through
the generic filesystem-oriented `wait_until()` helper after every released
virtual tick. The host now waits on the existing `_StepGate.cv` and receives an
explicit exception notification from the ticker thread. No scheduler behavior,
scenario, hold step, replica round, or assertion changed.

This is a small harness speedup, not a budget fix. The baseline already runs
far below the 300-second default per-file budget in
`scripts/run_tests_parallel.py`. The profiler's 20.476 seconds across 1,855
ticker waits counts time blocked on the ticker, not polling overhead the patch
removes.

The proof surface is `tests/e2e/core/delivery/test_cron_virtual_clock_soak.py`
in `python:3.14-bookworm` with two CPUs and a tmpfs temp directory. Baseline runs were
58.42s, 55.87s, and 55.55s; candidate runs were 53.69s, 57.87s, and 52.77s.
An independent reproduction measured base at 55.92s and 53.35s against the
candidate's 53.01s and 50.28s, about 3 seconds faster. All runs reported
`5 passed, 1 xfailed`; the C13 stats and replica assertions were unchanged. The
xfail is the pre-existing, separately owned open schedule oracle gap and is not
introduced by this patch.

Upstream contribution: not offered. The change applies cleanly to upstream's
`tests/e2e/core/delivery/_cron_clock.py` and is a candidate to offer there.

## Retirement

Retire this patch when the selected upstream release provides an equivalent
event-driven stepping harness. Also retire it, by restoring upstream's file, if
a baseline adoption makes it conflict, because the speedup alone does not
justify resolving a conflict.
