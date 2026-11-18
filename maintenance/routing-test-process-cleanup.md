# Routing e2e case process ownership

Fork patch identity: `routing-test-process-cleanup`.

## Why this fork keeps the patch

Source baseline: [`89f7c4e6495e7a13a0718b9640cd6f03711b172a`](https://github.com/0xble/hermes-agent/commit/89f7c4e6495e7a13a0718b9640cd6f03711b172a). This is a fork-local harness change; it has not been submitted to upstream. Adopt an upstream equivalent only when it owns the per-case boundary, not merely the finite CLI client's lifetime.

The routing truth-table harness gives each scenario a disposable HOME and HERMES_HOME. Its CLI subprocess already starts in a new session, and `run_hermes` kills that process group on return or timeout. A gateway started by `spawn_unmanaged_gateway(..., idle_exit=True)` is detached from that CLI process — a plain spawn creates another session, and on macOS the spawner may instead use a one-off launchd job — so the original group kill cannot reach it either way. Disposing of the test's temporary directory is not process teardown.

The historical `-p work chat -q` reproduction is no longer the normal path at this baseline: the finite CLI now starts a client-owned runtime. The regression explicitly invokes the real legacy idle-exit spawner in that profile-override leg, forcing its plain detached-spawn fallback so the test measures the session escape rather than launchd, and asserts the case owns its complete process lifetime. It does not claim the current unmodified finite CLI still reproduces the historical leak.

## Kept behavior

`tests/e2e/core/tenancy/_routing_processes.py` limits cleanup authority to same-user live process handles whose explicit HERMES_HOME resolves inside the exact scenario root. It never discovers or stops a host gateway, service, or profile. Lexical containment is checked before resolving a candidate home, so unrelated homes are not inspected through filesystem operations. Containment is rechecked before signaling; psutil handles bind signals to process birth identity, not a reusable PID alone.

Case teardown stops these owners before stopping the fake-provider fleet and egress trap. Termination has a bounded kill fallback, followed by a second scan for late children and a no-survivor assertion. Missing runtime PID records do not lose ownership of an importing gateway. An adjacent `case-other` root is never a child of `case`. Routing assertions, profile resolution, CLI timeouts, and the existing process-group cleanup remain unchanged.

The dedicated detached-daemon regression uses the test suite's explicit live-system-guard bypass marker because a reparented process is no longer in pytest's descendant tree. Its alternative signal authority is the exact disposable case-home invariant, which remains enforced before every signal. A failing regression still reaps only its own case processes in a finalizer after capturing the failure evidence.

## Scope and removal

This is test-harness cleanup, not a product idle-policy change. Unmanaged idle-exit probes deliberately treat an unavailable work store as busy. A gateway can therefore remain alive after its disposable store is removed; relying on a 600-second idle timer cannot establish case-level ownership. Do not relax those fail-closed probes merely to make a test daemon disappear.

Keep the patch until upstream owns detached process teardown at the same per-scenario boundary on both Linux and macOS. The test runner's Linux `/proc` cleanup at file exit is not equivalent to scenario teardown and does not cover macOS.

## Verification

Contract tests: `tests/e2e/core/tenancy/test_routing_process_cleanup.py`, `tests/e2e/core/tenancy/test_routing_truth_table.py`.

Run with an isolated outer home and the checkout's test interpreter:

```sh
HERMES_HOME="$PWD/.ci/test-home" HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/e2e/core/tenancy/test_routing_process_cleanup.py
HERMES_HOME="$PWD/.ci/test-home" HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/e2e/core/tenancy/test_routing_truth_table.py
HERMES_HOME="$PWD/.ci/test-home" HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/e2e/core/tenancy
```

The regression must fail without the case teardown hook, pass with it, and leave no process with HERMES_HOME under the scenario root on either path. Inspect the exact-head local gate and hosted qualification before treating the draft PR as review-ready. No host runtime change is needed to apply or verify this patch.
