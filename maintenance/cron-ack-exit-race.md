# Cron worker ack published just before a fast exit

Fork patch identity: `cron-ack-exit-race`.

## Required behavior

`_launch_external_cron_worker` in `cron/scheduler.py` waits for the detached
worker to publish `cron/external-workers/<execution_id>.ready` before handing the
run off. The wait loop checked the ack file and then polled the process. A fast
worker could publish its ack, finish, and exit between those 2 steps. The parent
then raised "cron external worker exited before ownership acknowledgement
(exit 0)" for a run that had completed, and discarded the completion as stale.
On 2026-11-23, under load averages of 38 to 100, this hit `flush-coalesced-pushes`.
Its execution had completed, but its `.ready` file was never consumed.

When the poll shows the worker has exited, the loop reads the ack once more
through the same `_consume_acknowledgement` helper the normal path uses. A valid
ack for this `execution_id` takes the acknowledged path. With no ack, or one for a
different execution, the dispatch failure is raised as before.

Contract tests: `tests/cron/test_restart_safe_worker_ack_race.py`.

## Upstream

Fork-only. No equivalent upstream fix has been established.
