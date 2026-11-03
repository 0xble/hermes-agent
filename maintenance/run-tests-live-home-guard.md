# Test-runner live-home guard

Load when changing the stale-environment fallback in `scripts/run_tests.sh`,
or the setup it reaches through `scripts/run-in-hermes-env`.

## Required behavior

Fork patch identity: `run-tests-live-home-guard`.

When the checkout test environment is missing or stale, `scripts/run_tests.sh`
refuses to re-exec `run-in-hermes-env` (and therefore refuses `setup-hermes.sh`
and pm) if the inherited home is a live runtime home. A home is live when any
of these hold on the home itself or on a root it resolves to. The profiles parent
is lexical. When the lexical and canonical roots differ, either live root counts:

- `$HERMES_HOME/current` is a symlink, including a dangling release pointer
- `gateway_state.json` exists
- `gateway.pid` or `gateway.b.pid` names a process `kill -0` can see, including
  EPERM from another user's gateway
- the home is the OS account default, or any path under it

Resolution matches `hermes_constants._hermes_root_of`: a home under the account
default resolves to that default; otherwise the profiles parent is lexical, so
`<root>/profiles/<name>` resolves to `<root>` even when that profile directory
is a symlink. Canonicalization matches Python `Path.resolve(strict=False)`,
including a missing suffix under a symlinked ancestor. If that names a
different root, the home is live when either root is. A resolution error is
live. PM writes the lexical root's `tools/`
(`pm/environments.store_root`).
The account default comes from the password database (`getent`, `dscl`, or
`~user`), not `$HOME` and not `/usr/bin/python3`. A failed lookup is live.

The refusal writes nothing to that home or its resolved root. It tells the caller to rebuild the
checkout-local venv with
`python -m pm.build_env --source . --out .venv --group dev --group test`,
or to set `HERMES_HOME` to a scratch directory. `HERMES_ALLOW_LIVE_HOME_SETUP=1`
is the only opt-in, and only for a deliberate setup against that home.

`hermes update` and the installer are unchanged. A scratch home, a dead pid
file, and a non-symlink file named `current` still take the fallback.

Upstream: no matching issue or PR in `NousResearch/hermes-agent` for this
fallback. The live release pointer and account-default home are fork runtime
layout; the guard stays here rather than in upstream setup.

## Verification and retirement

Contract tests: `tests/scripts/test_run_tests_live_home_guard.py`

Retire when the test runner no longer falls back into setup, or when setup
itself cannot write tool or test-env state into `HERMES_HOME`.
