# Updater external-supervisor sweep

## Required behavior

`hermes update` must never stop a gateway that declares an external supervisor,
including a replacement that launchd has started before the service-PID probe
has observed it. Truly manual `hermes gateway run` processes remain eligible for
the updater's stale-process sweep.

## Provenance

Fork patch identity: `updater-external-supervisor-sweep`. Own fork patch.

The update path in `hermes_cli/update_cmd_fleet.py` first excludes
`_get_service_pids(all_profiles=True)`, then treats every remaining gateway PID
as manual. Launchd can respawn `gateway run --external-supervisor` between those
probes. The 2026-10-07 field log showed the replacement PID 99851 at 19:05:46Z,
then a second launchd start after a 60-second throttle because the updater had
stopped the replacement. Existing gateway restart code already treats the
self-declared supervisor (control-socket identity or `--external-supervisor`
argv marker) as the ownership authority.

Related upstream prior art:

- [PR #121589](https://github.com/NousResearch/hermes-agent/pull/121589) detects
  externally supervised gateways when a deferred restart cannot reach them.
- [PR #133354](https://github.com/NousResearch/hermes-agent/pull/133354) protects
  the actual macOS gateway runtime beneath launchd wrappers during updates.

Those changes do not cover this fork's race where service PID discovery lags a
fresh launchd respawn in the manual sweep. This patch reuses the existing
external-supervisor predicate for mapped gateways and the explicit argv marker
for unmapped processes; an unreadable identity is treated conservatively and is
not converted into a destructive stop decision.

## Verification

Regression: `tests/hermes_cli/test_update_external_supervisor_sweep.py` uses a
fresh externally supervised PID plus a genuine manual PID while the service
probe returns no PIDs. It fails on the base because both PIDs enter the manual
kill set, and passes after the fix with only the manual PID stopped.

Focused command:

```text
PYTHONPATH=$PWD /Users/brianle/Repos/hermes-agent/.worktrees/adopt-upstream-review-fixes/.venv/bin/python -m pytest -q -p no:cacheprovider tests/hermes_cli/test_update_external_supervisor_sweep.py
```

No live Hermes update, launchd restart, or `~/.hermes` state was touched.

## Retirement

Retire when an upstream release makes the manual sweep consult the same
external-supervisor ownership predicate during the post-restart race and the
regression passes without this fork patch.
