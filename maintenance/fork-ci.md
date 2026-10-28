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

Shared update E2E signal injection must never enumerate or signal another file's
processes when CI lacks usable bubblewrap. The existing fixture HOME boundary
scopes the command agent's process table and each signal request. Fallback close
kills captured kernel process incarnations, including detached completion
children, before the leak check reaps them. Kernel PID namespaces remain in use
where available. NamespaceHost explicitly passes `HERMES_E2E_NAMESPACE_MODE`
through the agent invocation environment: `pid` preserves every PID-namespace
process even when its HOME changes or is absent; `home` alone enables fallback
HOME filtering. Missing/invalid mode or missing fallback HOME fails closed.
Controlled process-table regressions cover both modes without host signalling;
controlled host regressions load the actual NamespaceHost implementation with
local helper/reaper stubs, without importing Unix-only dependencies. All eight
controlled cases remain unmarked for Linux, macOS and Windows. Native Linux
regressions retain foreign-sibling refusal and detached cleanup.
No test is skipped and no worker count or timeout is relaxed.
The post-commit restart oracle follows the pause-record recovery warning rather
than the older fleet-restart wording, while still proving that the warning
clears only after a gateway serves the new commit.

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

The Codex host-binding catalog test keeps unrelated account-usage refreshes
offline. Otherwise its daemon can outlive the test, resolve a later test's pool,
and send an earlier key into that test's HTTP recorder. Host and exact-key
assertions remain unchanged. Dedicated usage tests still exercise real workers.

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

## Display watcher hosted-load race

The `fork-ci-reliability` identity also covers `tests/tui_gateway/test_display_watch.py`.
The process-wide lease watcher runs `_poll_runtime_files` every 0.5 seconds, while these tests
invoke the same pass directly. Under hosted load, the background pass could claim a launcher
liveness transition and update `_runtime_marks`, then spend time building the snapshot; the manual
pass returned before that broadcast, so the test saw no `display.status` event. The test helper now
serializes lease/runtime polls with one lock, preserving the real watcher and exact one-event
assertion while making the observation wait for the claimed broadcast. The external-start test also
holds that lock across its two start writes (`launcher.pid`, then `env`), so a background pass
cannot broadcast the half-written start as a separate transition. The runtime already treats
zombie launchers as dead in `tools/bot_desktop/runtime.py::_pid_alive`, so this is test-only
synchronization, not a production liveness correction. Retire when the watcher exposes an owned
synchronous poll/flush boundary or upstream makes this test isolation unnecessary.

## Update pause consumer publish race

The `fork-ci-reliability` identity also covers
`tests/hermes_cli/test_update_pause_boundaries.py::test_consumption_checkpoints_debt_before_unlink_and_producer_death`.
Its consumer subprocess published `consumed` with a plain `write_text`, while the test waited only
for the path to exist and then parsed it. Under hosted load the reader could open the file between
create and payload write and fail with `JSONDecodeError`. The subprocess now writes a sibling and
`os.replace`s it into place, so existence implies a complete payload. Product code is unchanged.
Retire with the test if upstream rewrites this cell harness.

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

## Desktop handoff fixture teardown

The `fork-ci-reliability` identity covers the sidecar-publication timeout cell in
`tests/scripts/desktop_update/test_handoff_delegate_publication.py`. After the
real 10-second A7 deadline refused delegate publication, the fixture released A7
then raced the handoff's natural exit with `os.killpg`. The live-system guard
correctly refuses a group whose leader has exited and whose remaining helpers
can no longer prove test ancestry. Let the real handoff finish and reap its
custodian after releasing A7, using the existing 10-second teardown bound.
Require exit code 3 and marker removal in addition to retaining the parked-update
and no-delegate assertions. A Linux scheduling probe that lets the handoff exit
while the guard checks ancestry reproduces the exact guard refusal on the old
fixture and passes on this cleanup. The guard and product are unchanged. Retire
when upstream uses cooperative teardown for this cell.

The Codex single-query approval cell now also requires a successful CLI exit and
prints its captured result before inspecting approval traffic. Empty traffic can
mean `session.create` failed before the fake app-server started, not that an
approval reply was lost. This is diagnostic strengthening, not a fix for cold
catalog discovery on the gateway's schema-generation path.

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

## Hosted load-progress fixture isolation

The `fork-ci-reliability` identity covers `tests/hermes_cli/test_load_progress.py`.
The tests inject events directly, but `get_loading_progress()` started the real
process-lifetime SSE watcher. With no managed endpoint, its first iteration
cleared the injected snapshot between event application and assertion. Hosted
run 38038101921, job 114172687324, failed with `assert 'm1' in {}`. A per-test
snapshot, disabled watcher startup, and a no-op `_clear_snapshot` (so a watcher
already started by another test in the same process cannot clear it) isolate
this unit boundary; the endpoint ownership and event-consumer assertions remain
unchanged. A forcing probe that
waits for the watcher's first clear failed 200/200 before and passed 200/200
after. Retire this adaptation when upstream isolates these direct-event tests.

## Hosted OpenRouter browser observation

The `fork-ci-reliability` identity also covers the nonce-path HTTP test in
`tests/hermes_cli/test_auth_commands.py`. Hosted run 38039556937, job
114176942229, raised `KeyError: 'genuine_status'`: returning the authorization
code means the callback server has accepted it, not that the browser worker has
published its response status. Join the real client thread with a bounded,
explicit failure before asserting its result, and surface client exceptions in
the test thread. If the loopback call fails, bounded cleanup attaches unfinished
worker and redirect-error diagnostics as notes to that same exception, preserving
its traceback instead of replacing it with an assertion. Both forged-path
rejection and genuine-path success assertions are retained. Delaying the client
after it receives the genuine response caused
188 failures in 200 iterations before and none in 200 afterward. No callback
port, server behavior, timeout policy, retry, or assertion was relaxed. Retire
this adaptation when upstream synchronizes the browser test's result owner.

## Metrics worker phase drain

The `fork-ci-reliability` identity also covers the suite's phase-boundary wait for
the gateway shared-metrics worker. Cron and gateway code hand each metric to one
background thread (`hermes_cli/observability/shared_metrics_gateway.py::_submit`).
That thread's first job lazily imports the metrics runtime, which registers named
loggers. Pytest starts every setup, call and teardown phase with
`catching_logs.__enter__`, and that method iterates the live
`logging.Logger.manager.loggerDict`. A job still running from the previous phase
raised `RuntimeError: dictionary changed size during iteration` at teardown of
`tests/cron/test_warning_execution_outcome.py` (hosted job 114104725116). The
next setup then cascaded into "previous item was not torn down properly". The
loop belongs to pytest, so `tests/conftest.py` drains the worker at the end of
each phase hook. `drain()` no longer starts a worker when none exists. Upstream
main has the same worker and loop. Retire this when upstream drains or joins the
worker in its suite, or when pytest copies `loggerDict` before iterating it.

## Hostile Git file budgets

The hostile Git commit and post-commit suites use bounded test files with at most
two expensive cells each. Shared setup and helpers live in `_hostile_commit.py`,
`_hostile_post.py`, and `_hostile_diagnostics.py`; underscored modules are not extra
runner tasks. Commit cells retain a fresh install per test, since they deliberately
corrupt their checkout. Post-commit cells retain module-scoped worlds within each
bounded file; the SIGKILL and interrupted-tail cells have their own worlds. All
17 original cells, marks, and assertions remain. The 900-second file bounds are
unchanged.

Phase markers identify installation, namespace startup/CLI/teardown, the active
cell, and sandbox mode. Passive watchdogs emit bounded owned-log tails at
180/360/540/720 seconds and Python stacks every 240 seconds, using original stderr
so timeout kills do not discard pytest-captured diagnostics. They never query a
potentially stuck namespace RPC.

A file split is not Linux qualification. Before hosted hostile signal cells run
without bubblewrap, require a verified namespace-containment prerequisite that
restricts enumeration, signaling, and teardown to fixture-owned processes only
in fallback mode, while preserving the full namespace process view under
bubblewrap. Without that prerequisite, keep the draft unqualified and block
hosted proof rather than dispatching unsafe cells or weakening them.

Compare complete test-function ASTs against the exact base (including decorators,
parameters, and assertions), verify that each of the 17 cells is collected once,
and run `scripts/run_tests.sh tests/scripts/test_upgrade_hostile_diagnostics.py`
through the canonical harness. On macOS the hostile cells retain their existing
Linux platform skips; this proves collection, not Linux behavior. Require safe,
exact-head hosted Linux proof before qualifying the regrouping. Retire this
adaptation when the accepted upstream harness independently schedules the cells
within the same finite budget without sacrificing fresh-install isolation.

## Test-Owned Session Provenance

The `fork-ci-reliability` identity also covers process ownership in the live-system
guard. A shell can orphan helpers before Python's first signal, while its owned
session leader remains alive. Live ancestry and a pre-signal descendant snapshot
then miss legitimate members of the group. Require every member's identity as
before, with POSIX session membership as an alternate origin proof only while the
same verified owned session leader is alive. Recheck both process creation times,
SID and PGID, reject members older than the leader, and never override a stale
cached identity. Shared foreign sessions, unverifiable processes and genuinely
unowned groups remain blocked. Previously verified identities continue to cover
leaderless groups without trusting a bare PID or PGID.

Run the live-system guard canary, process registry and PTY suites, plus the real
handoff publication fixture. The canary creates an inert orphan before the first
signal and covers stale identities, vanished leaders and changing membership.
Linux handoff behavior requires native Linux evidence, not a macOS platform skip.
Retire this adaptation when the accepted upstream guard proves the same owned
process topologies without allowing signals to unrelated live processes.

## Verification and retirement

The host-installation tripwire also protects the captured account's `.local/bin`
and `Library/LaunchAgents` from Python writes, removals and link publication.
Reads remain available for command and service discovery. Script/installer tests
own a temporary machine home through `HOME`, `Path.home()` and the current-UID
`pwd` lookup, and their ordinary subprocesses inherit that home. Ordinary tests
may opt into the same fixture when a real repair also publishes machine-level
CLI paths, including the doctor stale-iterations repair regression. Other tests
retain stable HOME semantics from upstream commit `ea8adb5e222d`; this is not a
sandbox for native children that discard the fixture environment.

Desktop smoke listener discovery on macOS intersects its existing ownership
candidates with the listening port before opening process descriptors, and skips
the native query when there are no candidates. This keeps unrelated host processes
outside fixture discovery while retaining the OS-listener and provenance checks.

Two disposable regressions were red on `cc277b4e2ffe`: host-installation paths
were mutable, and installer subprocess destinations used the operator's home.
Run `scripts/run_tests.sh tests/hermes_cli/test_real_home_tripwire.py
tests/scripts/test_installer_home_isolation.py` for the boundary, then the full
`tests/scripts` subsystem. Check the doctor opt-in with
`scripts/run_tests.sh tests/hermes_cli/test_doctor.py -k TestDoctorStaleMaxIterationsDrift`.
The Node gate covers `tests-js/scripts/desktop-smoke.test.ts`, including native
host-ledger ownership and candidate-scoped discovery. Related upstream [PR #111453](https://github.com/NousResearch/hermes-agent/pull/111453)
isolates individual host-service leaks, and [PR #76464](https://github.com/NousResearch/hermes-agent/pull/76464)
skips legacy Node link publication during recovery. Both are merged, but neither
replaces this shared write boundary. No new upstream contribution is claimed.
Retire this adaptation when the accepted upstream release protects those
destinations and installer machine homes through equivalent executable coverage.
Rollback removes the added host-directory registry, restores the guard's app-only
write callback, and removes the shared machine-home fixture, its consumers and
regression. Reverting candidate-scoped native discovery restores the prior smoke
helper without changing product runtime.

The qualified checkpoint `ca6782850432927f33df4775cb6dd45bb51460d2`
adds real-process Kanban and tenancy fixtures. Their OS home is separate from
their explicit Hermes home so native service discovery assigns a unique profile
label instead of finding the developer's host-wide default launchd gateway.
The service-ownership guard remains active. The approval-boundary matrix runs
GNU `rm` long flags on Linux, where that syntax is executable, while retaining
the portable destructive-command variants on macOS. These are test-harness
adaptations under `fork-ci-reliability`, not runtime-policy changes.

The classic CLI parity interrupt fixture holds its SSE response after the
`w040` progress witness instead of racing a finite response. Partial-line
streaming mirrors only its last 60 characters into a repainted status row.
A busy runner can miss the marker there and see it only after the completed
reply is committed, making the subsequent Ctrl+C correctly exit an idle CLI.
The held prefix keeps both the witness and running turn available until the
interrupt, without changing the alive, no-final-marker, or interruption-notice
assertions. `test_stream_partial_line_flush.py` covers real loopback SSE with
batched display observation. Retire this fixture adaptation when upstream has
an equally synchronized interrupt scenario. The live-session `w030` fixture has
the same held-response precondition, including a checked progress step before
Ctrl+C. The real-SSE/batched-display regression covers both drivers.

The shared classic CLI driver also masks spinner frames, not just digits, when
waiting for a quiet screen. An active approval or clarify modal changes its
spinner indefinitely even though the actionable UI is ready. Previously each
consumed the full, silently ignored 30-second settle bound. A native macOS
baseline reached `/quit after resume` with 294.3 seconds of successful steps
and then hit the unchanged 300-second file cap before its one-shot/cleanup.
Deadline failures now include the step, pane and gateway log, and stop further
input instead of accumulating later timeouts. A missing composer acknowledgement
never sends Enter. A slash-command completion redraw that leaves the typed
text on its own line still counts; a suggestion row that only names the
command does not. With that checked readiness condition, idle steps use a
0.5-second quiet period instead of a 2-second timing cushion. All output,
traceback, process and witness assertions remain. Non-TTY output is captured to
temporary files so inherited
output handles cannot hold pipe EOF open after the command exits or is killed.
Command wait and kill/reap waits remain bounded. Linux retains exact `/proc`
process matching, while macOS uses psutil's exact environment match to execute
the same suites natively. `tests/ci/test_classic_tmux_driver.py` covers animated
modals, changing content, failed synchronization and real inherited-output
descendants. It mocks the pane and needs no tmux, so it lives outside
`tests/e2e/core/cli_tmux`, where every suite must call `require_tmux()`.
Retire these driver adaptations when upstream supplies equivalent bounded,
condition-based synchronization and output capture.

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
