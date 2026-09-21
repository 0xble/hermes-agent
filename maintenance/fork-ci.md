# Fork CI reliability

## Required behavior

The routine fork Python lane runs every maintained unit's proof surface plus changed
Python test files when the change is limited to tests or documentation. Production,
shared fixtures, workflows, dependencies, deleted tests, missing diff context, and
other unclassified changes run full discovery. Selection never reports a full-suite
pass for smoke coverage. `scripts/ci/fork_test_surfaces.json` owns the explicit file
list. Update it when adding or moving a maintenance unit or its proof surface.

`CI` supports manual `workflow_dispatch` with `full_python: true`, selecting the
complete suite regardless of the diff. Explicit `full_python: false` dispatch runs
the maintained proof manifest without inferring any change scope. Missing PR/push
context still runs the full suite. `scripts/run_tests.sh` remains the canonical
complete upstream local gate. Fork full validation includes both `tests` and
`candidate-extensions`, since the canonical default discovers only `tests`. Neither
path skips failing tests. There is no new schedule.

The fork's configured runner is `ubuntu-latest` with four test workers. Full Python
jobs previously timed out at 30 minutes after 62–81% of coverage. Allow 90 minutes
for a complete run on this runner. Upstream's 96-core lane retains its 30-minute
limit. A longer timeout is capacity headroom, not proof of a passing full suite.
No protection rule or required status is removed. Other CI lanes are unchanged.

## Provenance and disposition

Fork patch identity: `fork-ci-reliability`.
Upstream design checked at `0ddeaf9334ff232a01612a51520a043db2cab77b` on 2026-09-21.
Root AGENTS and CONTRIBUTING require the canonical isolated runner and behavioral
regressions. The upstream runner assumes 96 cores; replicating that hosted cost
is unnecessary for this release-based personal fork. Repository CI is the owner,
not a runtime plugin or configuration setting.

The baseline runs 35623697079 and 35624448087 showed the same inherited failures.
The affected tests drifted from fork behavior: constructor state, profile-scoped
notification config, added Telegram handler, backup vanished-versus-error semantics,
cron timezone argument, pooled search-read instrumentation, and Telegram emphasis rendering. The quickstart test relied
on host RAM to produce a recommendation. Keep real constructors/profile routing and
pin hardware only at the probe boundary. Host-specific Cua launch tests exercise
Linux binaries and macOS app launches on their native hosts. Permission tests
resolve the temp-root alias before testing a new directory, preserving the
operator-owned symlink permissions policy. HTTP-mocked media tests also stub DNS
with a public address so host proxy routing cannot bypass or trip the real SSRF
guard. Readiness tests supply disk usage at the host probe, preserving the real
90% degradation calculation. Preserve unreadable-backup failure coverage
by injecting an actual archive-write error rather than treating a vanished file as
unreadable. Regenerate renderer vectors from the real oracle.

Related upstream searches for quickstart/hardware, memory_session_switch, conformance
vectors, watch drain, and background notification/profile semantics found no exact
replacement patch for the original CI failures. Later full local coverage found
the macOS Cua/temp-root issues also described by related upstream
[PR #110770](https://github.com/NousResearch/hermes-agent/pull/110770), open at
`f1ff453a1cdf56d95b42fa58c829231b64100b16`. Its Cua stubs corroborate the diagnosis.
The local permission fixture resolves its real path instead of mocking the
symlink guard, and Cua assertions run under explicit native OS markers. The
unrelated sitecustomize changes are excluded. No upstream contribution is claimed
for these fork adaptations.

The complete local run also exposed platform assumptions in fixtures. Canonical
path assertions preserve macOS symlink resolution; contributor collision tests
compare actual entries and contents on case-insensitive filesystems. Kernel
ownership counts real child process identities with psutil instead of GNU pgrep.
Native Linux markers scope GNU CLI and WSL tests. Unix socket tests use short,
owned temporary paths within Darwin's socket-name limit. Audio fixtures preserve
the Apple Silicon CPU safety policy and isolate the fake wake-word runtime.
Timer and remote-kernel concurrency fixtures use bounded Events to establish the
intended owner before asserting behavior; real worker threads and watchdogs remain
in place. Updater tests isolate all launchd inventory and profile-plist probes so fake
updates cannot discover the host gateway. No runtime safety guard is disabled.

The pre-fix full local baseline on macOS discovered 4,200 files: 49,601 passed,
74 failed, 560 skipped. Four updater files account for 14 failures, 26 failures
come from a separately owned Darwin native-search cleanup race, and 34 are the
host fixture defects addressed here. Focused reruns establish repaired fixtures;
the original full run is not reported as green. Candidate extension files are
additionally included in both bounded and full fork validation.

## Verification and retirement

Run all files listed in `scripts/ci/fork_test_surfaces.json`, including selector tests.
Run `actionlint` and `git diff --check`. Verify a hosted full run separately, recording
its exact revision and failures. Smoke success never closes complete-suite gaps.
Retire fixture adaptations when the accepted release tests already cover the maintained
behavior. Retain fork lane selection while runner/cost constraints differ from upstream.
