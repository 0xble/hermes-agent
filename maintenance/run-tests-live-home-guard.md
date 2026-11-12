# Test-runner live-home guard

Load when changing the stale-environment fallback in `scripts/run_tests.sh`,
or the setup it reaches through `scripts/run-in-hermes-env`.

## Required behavior

Fork patch identity: `run-tests-live-home-guard`.

When the checkout test environment is missing or stale, `scripts/run_tests.sh`
refuses to re-exec `run-in-hermes-env` (and therefore refuses `setup-hermes.sh`
and pm) if the inherited home is a live runtime home. A home is live when any
of these hold:

- `$HERMES_HOME/current` is a symlink, including a dangling release pointer
- `gateway_state.json` exists
- `gateway.pid` or `gateway.b.pid` names a running process
- the home is the OS account default (`pwd`, not `$HOME`)

The refusal writes nothing to that home. It tells the caller to rebuild the
checkout-local venv with
`python -m pm.build_env --source . --out .venv --group dev --group test`,
then run `env -u __HERMES_ACTIVATED HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh`
with their original test arguments. Rebuilding alone does not select `.venv`;
the explicit interpreter must contain pytest, and a stale activation must be
cleared for that command because activation takes precedence over `HERMES_PYTHON`.
The alternative is to set `HERMES_HOME` to a scratch directory.
`HERMES_ALLOW_LIVE_HOME_SETUP=1` is the only opt-in to setup against a live home,
and only for a deliberate setup against that home.

`hermes update` and the installer are unchanged. A scratch home, a dead pid
file, and a non-symlink file named `current` still take the fallback.

Upstream: no matching issue or PR in `NousResearch/hermes-agent` for this
fallback. The live release pointer and account-default home are fork runtime
layout; the guard stays here rather than in upstream setup.

## Verification and retirement

Contract tests: `tests/scripts/test_run_tests_live_home_guard.py`

Retire when the test runner no longer falls back into setup, or when setup
itself cannot write tool or test-env state into `HERMES_HOME`.
