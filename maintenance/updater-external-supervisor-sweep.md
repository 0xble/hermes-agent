# Updater external-supervisor sweep

## Required behavior

`hermes update` must not stop a gateway that an external supervisor started
after the restart phase took its pre-restart gateway snapshot, including a
replacement that launchd started before the service-PID probe observed it.

Gateways that were already running when the snapshot was taken keep the
existing update contract, even when they declare an external supervisor:

- A pre-existing mapped gateway with a custom supervisor, non-canonical launchd
  label, supervisord, s6, Docker, or another external manager is drained and
  handed back to that supervisor so it relaunches on the updated modules.
- A pre-existing unmapped `gateway run --external-supervisor` process is still
  signalled so its supervisor can relaunch it on the updated code.
- Truly manual `hermes gateway run` processes remain eligible for the updater's
  stale-process sweep.

If the mapped-gateway ownership probe raises, the updater prints a warning that
names the PID and profile. A post-snapshot process is protected; a pre-existing
process stays on the drain and hand-back path.

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

The restart phase already captures `pre_restart_gateway_pids` before any
systemd, launchd, or manual gateway is touched. The sweep uses that snapshot to
separate a fresh supervisor child from a pre-existing supervised gateway that
still runs old modules (#88654). If the snapshot is unavailable (`None`), the
sweep cannot prove freshness and protects every self-declared supervised
gateway rather than risking the launchd throttle race.

Related upstream prior art:

- [PR #121589](https://github.com/NousResearch/hermes-agent/pull/121589) detects
  externally supervised gateways when a deferred restart cannot reach them.
- [PR #133354](https://github.com/NousResearch/hermes-agent/pull/133354) protects
  the actual macOS gateway runtime beneath launchd wrappers during updates.

Those changes do not cover this fork's race where service PID discovery lags a
fresh launchd respawn in the manual sweep.

## Verification

`tests/hermes_cli/test_update_external_supervisor_sweep.py` covers:

1. A fresh launchd respawn absent from the snapshot is not signalled. This fails
   on the pre-patch base.
2. A pre-existing mapped externally supervised gateway is drained and reported
   in `externally_supervised_profiles`.
3. A pre-existing unmapped `--external-supervisor` gateway is still signalled.
4. A truly manual gateway is still stopped.
5. An ownership-probe exception prints a warning and protects a fresh PID.

Focused command, from the repository root with any interpreter that has the
project's test dependencies installed:

```text
PYTHONPATH=$PWD python -m pytest -q -p no:cacheprovider tests/hermes_cli/test_update_external_supervisor_sweep.py
```

No live Hermes update, launchd restart, or `~/.hermes` state was touched.

## Retirement

Retire when an upstream release excludes only post-snapshot externally
supervised gateways from the manual sweep, while still handing back pre-existing
ones, and this regression file passes without this fork patch.
