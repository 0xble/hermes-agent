# Cron dependency timeouts

Patch identity: `cron-dependency-timeout`.

## Behavior

A detached cron run that times out waiting on a dependency names that dependency (the
gateway handshake or the model proxy) instead of reporting a bare `TimeoutError: timed out`.
A model-proxy outage that stalls several jobs at once opens one shared incident rather than one
per job. A job's own wall-clock timeout still belongs to that job.

A timeout is named a model-proxy outage only when the exception chain carries the HTTP request
that timed out and that request went to the configured loopback proxy. A loopback `base_url`
alone is not proof: a bare `TimeoutError: timed out` from a tool or the job's own code stays a
`timeout` incident for that job.

The shared incident (`job_id="dependency"`) follows the ordinary alert rule: only an `alerted`
row (a notice actually reached the operator) inside `cron.failure_repeat_alert_hours` quiets the
other jobs. A `detected` row is never withheld, so an undelivered first notice cannot silence
the outage. No single job's recovery resolves the shared row, so an open one that has seen no
failure for `DEPENDENCY_OUTAGE_GAP_SECONDS` re-opens as `detected` on the next failure and pages
again as a new outage.

Source surfaces: `cron/dependency_timeout.py`, `cron/incidents.py`, `cron/scheduler.py`,
`cron/scheduler_authority.py`, `hermes_cli/gateway_client.py`,
`hermes_cli/gateway_runtime_discovery.py`. Landed from
https://github.com/0xble/hermes-agent/pull/1271.

## Retirement

Retire when upstream names the stalled dependency in detached cron failures and deduplicates a
shared dependency outage into one incident. Roll back by reverting this logical patch.

Contract tests: `tests/cron/test_dependency_timeout.py`.
