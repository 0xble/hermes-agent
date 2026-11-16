# Cron attended-model isolation

- **Patch identity:** `cron-attended-model-leak`
- **Contract tests:** `tests/gateway/test_attended_turn_runner.py::test_cron_execution_never_switches_to_attended_route`
- **Behavior:** The gateway's attended-model switch runs at each provider request boundary. It never rewrites an agent while a cron execution identity is active, because cron resolves its route at construction (`cron.model`, the job's own `model`, or an alias such as `attended` that a job pins explicitly). Launch-policy sessions, subagents and auxiliary agents, and sessions pending bulk reset are exempt for the same reason: each owns its own route. The exemptions live in one helper, `attended_switch_exempt`.
- **Surfaces:** `gateway/run_turn_runner.py` (`_apply_attended_before_api_call`) and `gateway/run_turn_runner_attended.py`.
- **Retirement:** Remove this patch and its focused test when upstream skips attended routing for cron executions.
- **Rollback:** Revert the commits carrying `Fork-Patch: cron-attended-model-leak`.
