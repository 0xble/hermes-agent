# Fork CI reliability

## Required behavior

`bin/ci` owns contributor setup and the complete source gate. Its Python lane
runs both `tests` and `candidate-extensions` through the canonical isolated
`scripts/run_tests.sh` harness. A selected lane is partial evidence. The gate
does not use changed-file classification or substitute a smoke manifest for
complete discovery.

See [portable-ci.md](portable-ci.md) for tool versions, worktree isolation,
coverage allocation, native qualification and release boundaries. Contributors
need no personal tooling or publisher credentials. The standalone runner binds
results to the selected candidate, owns cancellation and timeouts, and publishes
the existing required `local-ci/full` check for ordinary GitHub merges.

All nine nonrelease Node checks remain. As in PR #50, the source gate excludes
signed desktop packaging, whose stamp requires release context. Packaging still
belongs to release qualification. Unknown skip labels and invalid concurrency
must fail rather than narrow coverage silently.

## Provenance and disposition

Fork patch identities: `fork-ci-reliability`, `ci-gate-cancelled-blocks`.

Fork-Patch-Backfill: ed3b7d08dfcd7475238cf8ec4c946a9b9ac9ddf6; fork-ci-reliability

`ci-gate-cancelled-blocks` owns the requirement that interruption cannot count as
success and that the Fork-Patch contract is checked. The portable static lane
enforces trailers, while the standalone runner rejects interrupted results.
The old Actions gate counted only `failure`, so `cancelled` fell through to success. A job that
exceeds `timeout-minutes` is recorded as cancelled, which is how #42, #51 and #53 each
reported all checks passing after being cut off mid-suite at the 30-minute cap this unit
raised. The trailer rule landed in #47 as a client-side commit-msg hook, which a squash
merge composes past; #48 merged one commit later with no trailer (repaired in #55 via
`Fork-Patch-Backfill`). The old Actions implementation can retire when the
portable static check and runner cancellation behavior are verified. Preserve
the behavior and this identity's historical ownership across that replacement.
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
The desktop installer-ownership fixture waits for a readiness message from its real
Python child before checking process arguments. Full platform concurrency left the
shell wrapper running beyond its former 40 x 25ms polling window. A bounded child
handshake preserves the real process-identity assertions and reports startup failures.

The portable Linux sandbox exposed a browser port-allocation bug when IPv6 is
unavailable. Upstream [PR #91455](https://github.com/NousResearch/hermes-agent/pull/91455),
commit `c97962cd883e0da52ed821cf6c145eb1b938f5c4` by Petr Bohac, addresses the same
failure. Its patch was cherry-picked without committing and reconciled with the
current function layout. Probe actual IPv6 loopback availability, then require
exclusive binding on available families. Retain the upstream regression and
author credit in delivery. A real occupied IPv4 socket reproduced the bug before
the adaptation and was skipped afterward, both with simulated unavailable IPv6
and with the host's native dual stack. Full sandbox verification remains separate.

Doctor diagnostics also replaced explicitly selected remote terminal backends
with `local` when running inside a container. The narrow correction from
upstream [PR #94233](https://github.com/NousResearch/hermes-agent/pull/94233),
commit `3a38547622af647df999d9f9b2ea6f7450678b61` by Kyzcreig, preserves that
selection. The container informational message applies only when the selected
backend is local. Explicit Docker checks remain unchanged. The existing Vercel
diagnostic and secret-redaction test covers both container-probe outcomes.

Other portable-run failures came from fixtures inheriting container policy or
clearing their isolated home, and from inherited assertions predating deliberate
fork behavior. Permission and host-service fixtures now select their intended
host policy while retaining separate container-policy tests. Feishu persistence
uses an owned home and real atomic writing off the event-loop thread. Update
notification tests require truthful unverified-runtime messaging, bounded output
delivery and marker cleanup, rather than claiming the old runtime is healthy.
Cron alerts retain the upstream plain-language notice and cron-specific fallback
guidance. These repairs require fresh sandbox proof before migration acceptance.

The pre-fix full local baseline on macOS discovered 4,200 files: 49,601 passed,
74 failed, 560 skipped. Four updater files account for 14 failures, 26 failures
come from a separately owned Darwin native-search cleanup race, and 34 are the
host fixture defects addressed here. Focused reruns establish repaired fixtures;
the original full run is not reported as green. Candidate extension files are
additionally included in both bounded and full fork validation.

## Verification and retirement

Run `bin/ci` and `git diff --check`. Record the exact candidate, environment,
completed lanes and failures. Focused regression success never closes a
complete-suite gap or proves an installed runtime.
Retire fixture adaptations when the accepted release tests already cover the maintained
behavior. Retire superseded hosted orchestration after its replacement qualifies.

## Portable source gate migration

The `fork-ci-reliability` implementation now has a repository-owned `bin/ci`
entrypoint. See [portable-ci.md](portable-ci.md) for invocation, exact tool pins,
worktree isolation, complete allocation of the 36 workflows, and residual native
OS/integration/release lanes. The candidate removes hosted orchestration while
remote enforcement remains until replacement qualification and cutover. A partial
lane or a Linux pass cannot establish all-platform coverage.
