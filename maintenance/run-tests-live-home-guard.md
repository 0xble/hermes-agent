# Test-runner live-home guard

Load when changing `scripts/_setup_guard.sh`, the stale-environment fallback in
`scripts/run_tests.sh`, or setup reached through `scripts/_activation.sh` and
`scripts/run-in-hermes-env`.

## Required behavior

Fork patch identity: `run-tests-live-home-guard`.

When the checkout test environment is missing or stale, `scripts/run_tests.sh`
refuses to re-exec `run-in-hermes-env` (and therefore refuses `setup-hermes.sh`
and pm) if the inherited home is a live runtime home. `hermes_sync` also calls
the same `hermes_require_safe_setup` check before invoking setup, so direct
`hermes_sync` and `hermes_ensure_env` calls cannot bypass the runner's refusal.
`scripts/_setup_guard.sh` owns marker checks and refusal. Its read-only Python
bridge, `scripts/setup_guard.py`, calls the actual runtime path owners.
Sourcing that library changes no shell options and performs no writes.
A home is live when any of these hold on the home itself or on a root it resolves to. The profiles parent
is lexical. When the lexical and canonical roots differ, either live root counts:

- `$HERMES_HOME/current` is a symlink, including a dangling release pointer
- `gateway_state.json` exists
- `gateway.pid` or `gateway.b.pid` names a process `kill -0` can see, including
  EPERM from another user's gateway
- the home is the OS account default, or any path under it

The bridge calls `get_hermes_home()`, `get_default_hermes_root()`,
`account_default_hermes_home()`, and PM's `dependency_home_root()` / `store_root()`.
It also checks setup's pre-PM uv store. It runs in the repo cwd setup uses, after
the same Python environment sanitization, with the interpreter activation passes
to setup via `--bootstrap-python`. Tilde, environment expansion, whitespace and
profile rules therefore belong only to runtime code, not a shell imitation.
Every lexical and resolved path is checked for markers, without equality skips;
symlinked profile ancestors retain both root interpretations. Account-default
containment uses the runtime function and pathlib. Resolution, interpreter or
import failure refuses. Python 3.13+ lenient resolution can retain a symlink loop;
the bridge probes path ancestors so ELOOP and access errors still fail closed.
There is no pre-bootstrap shell fallback; an intentional
first bootstrap without a usable interpreter requires the explicit opt-in.

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
