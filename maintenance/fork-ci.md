# Fork CI reliability

## Required behavior

The routine fork Python lane runs every maintained unit's proof surface plus changed
Python test files when the change is limited to tests or documentation. Production,
shared fixtures, workflows, dependencies, deleted tests, missing diff context, and
other unclassified changes run full discovery. Selection never reports a full-suite
pass for smoke coverage. `scripts/ci/fork_test_surfaces.json` owns the explicit file
list. Update it when adding or moving a maintenance unit or its proof surface.

`CI` supports manual `workflow_dispatch` with `full_python: true`, selecting the
complete suite regardless of the diff. `scripts/run_tests.sh` remains the canonical
complete local gate. Neither path skips failing tests. There is no new schedule.

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
pin hardware only at the probe boundary. Preserve unreadable-backup failure coverage
by injecting an actual archive-write error rather than treating a vanished file as
unreadable. Regenerate renderer vectors from the real oracle.

Related upstream searches for quickstart/hardware, memory_session_switch, conformance
vectors, watch drain, and background notification/profile semantics found no exact
replacement patch. No upstream contribution is claimed for these fork adaptations.

## Verification and retirement

Run all files listed in `scripts/ci/fork_test_surfaces.json`, including selector tests.
Run `actionlint` and `git diff --check`. Verify a hosted full run separately, recording
its exact revision and failures. Smoke success never closes complete-suite gaps.
Retire fixture adaptations when the accepted release tests already cover the maintained
behavior. Retain fork lane selection while runner/cost constraints differ from upstream.
