# Retired supervisor check-ins

Fork patch identity: `supervisor-checkins`.

Retired on 2026-11-27 because the feature was off everywhere: `checkins.enabled: false` in the
live `config.yaml`, in both profile configs and in all 13 `config.yaml.bak*` snapshots. The live
`state.db` held no `checkin:` timers, no non-terminal native admissions carrying a supervisor
check-in, and no delivery obligations marked `supervisor_checkin`.

Removed: the idle-session scanner and its cadence state (`gateway/run_checkins.py`,
`hermes_cli/checkins.py`), the delivery fence and disarm provenance (`gateway/checkin_delivery.py`),
the `checkins` config defaults, the `supervisor_checkin` turn handling in silence, progress,
streaming, inactivity-warning and failed-turn paths, the failed-boundary rewrite
(`replace_failed_turn_boundary`), the automation `checkin` descriptor metadata, the delegation
roster query, the user-guide page and the exclusive tests and daemon fixtures.

Legacy state: a leftover `checkins:` block in `config.yaml` is ignored. The
`delivery_obligations.supervisor_checkin` column stays in the schema. Recovery abandons any row
that still has it set, with a warning, instead of delivering it. Persisted user rows with
`display_kind: supervisor_checkin` remain classified as runtime-generated prompts.

Kept: goal peer waits (`/goal peer wait|resolve|list`, `GoalState.pending_peers`, the
`wait_on_peer` and `resolve_peer` controls and the refusal to complete a goal while peers are
pending) entered under this identity but work without check-ins and are reachable from live goal
commands. They stay as goal behavior, covered by `tests/hermes_cli/test_goal_peers.py`,
`tests/gateway/test_goal_peer_rpc.py` and `tests/gateway/test_goal_peer_commands.py`. Only their
check-in arming and roster entries were removed. This record stays so published `Fork-Patch`
trailers keep an accountable owner.

Contract tests: none, retired.
