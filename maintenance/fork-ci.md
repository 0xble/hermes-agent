# Fork CI reliability

## Required behavior

The current CI interface is owned by [repository-ci-contract.md](repository-ci-contract.md);
this unit keeps the reliability rules that every profile must still satisfy. Two profiles
carry different evidence and must not be confused:

- **PR gate** (`./bin/ci gate <sha>`, run by `.github/workflows/gate.yml`, aggregated as the
  required `qualification` check): static policies, the focused Python files listed in
  `GATE_PYTHON_FILES` in `scripts/ci/portable.py`, and the bounded Node checks. It is
  deliberately partial. A gate pass is not complete-suite evidence.
- **Complete profile** (`./bin/ci nightly <sha>`, `.github/workflows/nightly.yml`, and the
  local `./bin/ci full`): the canonical `tests` tree through the isolated
  `scripts/run_tests.sh` harness plus all nine nonrelease Node checks, E2E, docs, Rust and
  container lint. Claims of complete coverage need this profile, not the gate.

Profile-owned plugins are tested from their canonical source repository, not from this
Hermes application checkout. A selected lane (`check --lane`) is partial evidence. No
profile uses changed-file classification, and the complete profile never substitutes a
smoke manifest for complete discovery.

Portable Python execution passes `--file-retries 0`, including E2E and native OS
qualification. A fail-once test stays failed at the gate entrypoint. Interactive
runner defaults remain unchanged. `scripts/ci/tests/test_portable.py` exercises
both outcomes with a real pytest file and a persistent attempt counter.

The nightly fetches upstream CalVer release tags before its historical upgrade E2E:
`actions/checkout` sees only fork tags (latest `v2026.8.3`), otherwise the test's
`git describe HEAD~1` stages an obsolete updater instead of the previous release.
The test still uses a local origin with no network and seeds a v45 user config to
exercise the MCP disabled → enabled migration even when the release binary is v46.
A missing upstream tag fetch fails the nightly rather than silently narrowing coverage.

See [portable-ci.md](portable-ci.md) for tool versions, worktree isolation,
coverage allocation, native qualification and release boundaries. Contributors
need no personal tooling or publisher credentials. Both exact-SHA profiles bind
results to the selected candidate and fail closed on cancellation or timeout.

As in PR #50, no profile runs signed desktop packaging, whose stamp requires release
context; it belongs to release qualification. Unknown skip labels and invalid concurrency
must fail rather than narrow coverage silently.

Historical: before the gate/nightly split, a standalone runner published a required
`local-ci/full` check that ran the complete profile on every PR, including all nine Node
checks. That check is no longer required; `qualification` replaced it.

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

## Release-sync scope

A release sync merges thousands of upstream commits. Fork policy checks classify
fork work only, so contributor attribution (`scripts/ci/portable.py`), catalog
admission (`scripts/ci/check_plugin_admission.py`) and the trailer check
(`scripts/check_fork_patches.py`) exclude history reachable from the accepted
release baseline in `MAINTENANCE.md`, read by `scripts/ci/release_baseline.py`. A
catalog entry is skipped only when byte-identical to that release; a fork edit is
still admitted. Without this the v2026.9.24 gate failed on upstream contributors
unmapped in the fork, and on an upstream catalog pin whose repository is gone.

The same sync exposed an upstream desktop build bug: `xcrun` honors an inherited
`SDKROOT` and exits 72 on a stale one even when `-isysroot` names a valid SDK, so
`macos-sysroot-native.test.mjs` failed on its stale-SDKROOT rung. The helper
builds now pass `xcrunEnv()`, which drops `SDKROOT` after the resolver has chosen
the SDK. Offer this upstream; drop the patch once upstream carries an equivalent.
