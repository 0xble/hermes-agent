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
runner defaults remain unchanged. The Windows Desktop cwd self-test retains its
60-second limit; on a timeout it snapshots the PowerShell parent exit state
and live descendant PIDs/names/statuses, kills the snapshotted process tree,
then performs a bounded drain to capture partial stdout. If the drain still
times out, it raises the snapshot without closing a pipe held by a reader thread.
Nightly 36492811025 timed out inside Python's stdout reader-thread join,
without child output or process-state evidence. The exact test and updater script
were unchanged from two earlier successful Windows lanes, so the runner-versus-
child cause remains unknown. This diagnostic tests the next failure boundary,
not a claim that process inheritance caused that run. Upstream issue #95971
records a different updater fixture hanging on hosted Windows; no product change
or changed timeout is justified by this run alone.
`scripts/ci/tests/test_portable.py` exercises
both outcomes with a real pytest file and a persistent attempt counter. The slow
summary E2E explicitly configures `compression.abort_on_summary_failure: true`
when asserting no archived history: with the default false, a second stalled
summary route intentionally commits a deterministic fallback without using the
failed model's output. Nightly run 36456019275 recorded the slow-mode
archiving assertion, but its test output does not expose the summary route; the
existing deterministic fallback implementation explains why the assertion can
fail. The abort-mode test still checks that a cancelled or late summary cannot
archive history. The Desktop tenancy E2E retains its 150-second cron deadline
but, on failure, reports per-profile request counts, heartbeat/success age, and
canary job state (no prompts or keys) so the next missed fire can be attributed
to enumeration, ticker liveness, claim, or execution instead of an opaque timeout.

The nightly fetches upstream CalVer release tags before its historical upgrade E2E:
`actions/checkout` sees only fork tags (latest `v2026.8.3`), otherwise the test's
`git describe HEAD~1` stages an obsolete updater instead of the previous release.
The test still uses a local origin with no network and seeds a v45 user config to
exercise the MCP disabled → enabled migration even when the release binary is v46.
A missing upstream tag fetch fails the nightly rather than silently narrowing coverage.

The macOS immutable-release plugin rejection matrix builds one real checkout and
candidate venv, then tests each plugin kind through the existing-candidate smoke
path. The previous five parametrized cases each independently rebuilt the entire
web UI and venv. On hosted macOS this exhausted the 300-second per-file bound
partway through the fourth case, not in launchd. Reusing a ready, immutable
candidate preserves each kind's import rejection, unflipped pointer, and partial
receipt without repeatedly testing unrelated build tooling.

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

Fork patch identities: `fork-ci-reliability`, `ci-gate-cancelled-blocks`, `terminal-heavy-slot`.

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

## Nightly Linux notification regression

The `fork-ci-reliability` identity covers the cut-completion notice assertion in
`tests/tools/test_notify_on_complete.py`. The formatter intentionally ends every
completion with `PROCESS_NOTIFICATION_END`; the older test still required the
payload's closing bracket to be the final character. Assert the exact bounded
output and framing terminator together. The nightly Linux failure was a stale
test expectation, not a truncated-notice product defect.

## Git fixture isolation

The `fork-ci-reliability` identity also covers the real-Git fixtures in
`tests/hermes_cli/test_worktree_gc.py`, `test_worktree.py`, `test_gitlock.py`,
`test_goal_gates.py`, and `test_update_skip_unchanged_editable_install.py`.
A redirected `HOME` alone does not isolate an explicit `GIT_CONFIG_GLOBAL` or
an inherited XDG config path: signing and lefthook settings can stop fixture
commits before their behavioral assertions run. These fixtures set the global
config to `os.devnull`, disable system config, and redirect XDG config into
`tmp_path` for Git subprocesses and their production-code callers. The canonical
runner retains the CI-provided safe.directory config; changing its global
allowlist would discard that boundary and would not protect direct pytest runs.
Upstream main at `cb3142d3257b15ac42de544bd585793567400d0f` has the same
worktree-GC fixture leak; [upstream contribution #124019](https://github.com/NousResearch/hermes-agent/pull/124019)
uses only the single shared test file. [Fork delivery #173](https://github.com/0xble/hermes-agent/pull/173)
also repairs the four sibling test files that fail under the same poison config.
A poison global config with `commit.gpgsign=true` fails setup before the patch,
and all 95 affected tests pass after it. Retire the fork adaptation when a
released upstream revision provides the same hermetic tests.

## Audio playback isolation

The `fork-ci-reliability` identity also covers test audio isolation. The autouse
`_audio_playback_guard` stubbed only `hermes_cli.voice`, but the streaming TTS
pipeline late-imports `tools.voice_mode.play_audio_file`. On macOS,
`TestStreamTtsToSpeaker::test_none_sentinel_flushes_buffer` synthesized "Hello
world." through keyless Edge TTS and played it with `afplay` on every run. The
guard now also stubs `tools.voice_mode.play_audio_file` and
`_play_int16_via_tempfile`. The sentinel test fakes synthesis and asserts one
playback. Tests that exercise playback with mocked backends opt out through
`@pytest.mark.real_audio_playback`. This adopts upstream
[PR #111089](https://github.com/NousResearch/hermes-agent/pull/111089) by
yanglei070-ux for [issue #88898](https://github.com/NousResearch/hermes-agent/issues/88898).
The adaptation keeps the fork's portable `TestPlayBeep` and Linux-only WSL2
class, and omits upstream opt-out tests the fork already removed. A logging
`afplay` shim on `PATH` recorded one call on the base and none with the patch.
Retire when a released upstream revision carries equivalent guard coverage.

## Linked worktree test venv

The `fork-ci-reliability` identity also covers the canonical runner's venv
probe. `scripts/run_tests.sh` looked only at the checkout's own `.venv`/`venv`
and the release venv, which has no pytest. A linked worktree under
`.worktrees/<name>` therefore exited "no virtualenv with pytest found" unless
the caller exported `HERMES_PYTHON`, and exact-candidate reviewers inspecting
worktrees approved from static reading. The runner now also probes the primary
checkout's `.venv`/`venv`, found through `git rev-parse --git-common-dir`,
after the worktree-local ones. `scripts/ci/tests/test_python_scratch.py`
builds a real linked worktree whose only pytest-capable venv is in the primary
checkout, and fails on the previous probe order. Upstream main (after
`v2026.9.24`) replaces the probe list with per-checkout activation
(`scripts/_activation.sh`, `activate`), which builds a test environment for
each worktree. Retire this probe when the fork adopts a released upstream
runner with that activation.

## Verification and retirement

The qualified checkpoint `ca6782850432927f33df4775cb6dd45bb51460d2`
adds real-process Kanban and tenancy fixtures. Their OS home is separate from
their explicit Hermes home so native service discovery assigns a unique profile
label instead of finding the developer's host-wide default launchd gateway.
The service-ownership guard remains active. The approval-boundary matrix runs
GNU `rm` long flags on Linux, where that syntax is executable, while retaining
the portable destructive-command variants on macOS. These are test-harness
adaptations under `fork-ci-reliability`, not runtime-policy changes.

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
