# Cron Script Failure Status

**Patch identity:** `cron-script-failure-status`.

## Preserve

An agent-backed job's pre-run script failure remains a run failure even when the
agent completes normally or returns a silence marker. Keep running the agent so
it can explain the failure. Preserve the script runner's redacted exit code and
reason in the existing run error and job `last_error`, including when agent
setup or execution also fails. No job schema or configuration change is needed.

`wakeAgent=false` and empty output skip the agent only after a successful script.
There is no advisory-exit option. Script-only jobs retain their existing failure
classification. Failed agent-backed runs use the ordinary failure delivery lane,
incident handling, execution ledger, status and failure streak bookkeeping.

## Verify and retire

Contract tests: `tests/cron/test_script_failure_status.py`.

Run the cron suite with `scripts/run_tests.sh tests/cron`. The regression uses
real scripts and the scheduler's run/save/compose/bookkeeping path with a local
agent double and captured outbound queue. Cover exit 1, timeout, normal and
silent agent responses, failed-script wake suppression, exit 0 and successful
wake suppression. Keep existing script termination/tree-reaping coverage.

Upstream https://github.com/NousResearch/hermes-agent/pull/20323 addresses the
same root cause but remains open. The separate failure-policy proposal at
https://github.com/NousResearch/hermes-agent/pull/123508 is closed and is not an
existing advisory-exit contract on this fork's base.

Retire when upstream preserves these contracts through the full agent-backed
run path. Rollback reverts the commit carrying
`Fork-Patch: cron-script-failure-status`. Do not rewrite historical run records.
