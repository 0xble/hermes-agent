# Disposable one-shot gateway lifetime

Fork patch identity: `disposable-home-gateway-lifetime`.

## Contract

A finite CLI client (`-z`, `-Q`, stream-json and noninteractive `-q`) that
actually auto-starts an unmanaged owner holds a private lifetime pipe. Closing
the connection or exiting the client closes the pipe. Client SIGTERM/SIGKILL
also produces EOF. The gateway watches before heavyweight startup and requests
shutdown, with a three-second hard self-exit bound if shutdown wedges. Before
signaling, reuse only the gateway process's already-loaded foreground/background
tool registries to kill its own detached tool children (no host-wide PID scan).
The session-only startup retains the ordinary bounded MCP-discovery barrier so
the first finite turn does not lose eager MCP tools. Only the
creating client's Popen handles confer cleanup authority; discovered owners and
installed services are never stopped. Interactive sessions keep the existing
reusable idle-exit behavior. The classic `HermesCLI` route forwards its existing
`HERMES_SINGLE_QUERY_SESSION` marker at `GatewayConnection`'s lazy dial, so finite
`chat -q`/`-Q` uses the same lease as `-z`; no new configuration or lifecycle
manager is introduced. Injected connection factories are unchanged.

Update pause must not acquire a finite client's lease: `pause-for-update` refuses
with `{"pausing": false, "client_owned": true}`. The shared
`hermes_cli/gateway_client_ownership.py` predicate proves ownership with a
PID-matched control identity or exact `--client-owned` argv flag. POSIX bare
pause, the same update's manual/profile restart arming, unmapped SIGTERM sweep,
stale-fleet drain and final force-kill all exclude those owners. Windows pause
filters them before planned-stop markers, pause debt and argv replay; its
leftover tree-kill selection also excludes them. One notice per excluded PID
explains that it remains on its lease. Such owners are never drained, SIGTERMed,
SIGKILLed, replayed with detached stdin, or added to update resume debt; they
keep running old code until their creating client's lease ends. Ordinary
persistent gateway pause/replay and restart behavior is unchanged.

Contract tests: `tests/gateway/test_client_owned_update_pause.py`,
  `tests/hermes_cli/test_update_client_owned_pause.py`,
  `tests/hermes_cli/test_update_client_owned_fleet.py`.
The Windows marker/leftover selections have host-independent fake-process
coverage; its full only-a-lease pause regression is marked for native Windows.

Fleet-review preflight: root/CLI/tests guidance and CONTRIBUTING rechecked at
upstream `e76fbd80be65ea5c1b89831f8d7aaedb713f5cac`. Independent proposal:
extend the existing identify/argv decision into one neutral predicate and use
it at every updater stop/replay selection, not a second lifecycle manager.
Open/closed PR and issue searches for client-owned gateways and finite update
leases found no equivalent; neighboring fleet restart fixes address process
identity and supervised relaunch, not finite ownership. This remains an
intentional fork ownership fence with no added dependency.

The finite-client owner serves local sessions only. It must not dispatch cron,
restore heartbeat/loop/recovered work, connect messaging adapters, or start
long-lived daemon housekeeping. A disposable home is not a daemon enrollment.

## Design and upstream

Independent proposal: option (a), a private stdin pipe rather than PID polling
or a host-wide scheduler. A short idle timer still fails closed on persistent
work or a deleted home; a host cap adds coordination without establishing
ownership. The pipe also covers death while gateway imports are wedged.

Upstream guidance pinned at `e8639fd4312e5102d6f10c76b56406cb01c19092`:
root and subsystem AGENTS plus CONTRIBUTING. Searches covered open/closed PRs
and issues for idle-exit, one-shot gateway and gateway lifetime. Related upstream
PR NousResearch/hermes-agent#145864 handles a deleted store poisoning idle-exit
probes, not finite client lifetime. PR #143630 addresses inherited launchd markers,
not ownership. Neither eliminates the detached lifetime after successful client
exit. No equivalent caller-death fence found. Intentional fork divergence until
an equivalent upstream lifetime and daemon-service fence ships.

## Review corrections: ownership and crash state

The runtime has one writer per canonical home: `gateway/runtime_ownership.py`
uses nonblocking exclusive reservations of `gateway.lock`, and
`gateway/run_bootstrap.py` reserves profiles and claims the PID file before
opening session authorities. Two active gateways cannot safely coexist on one
home. A competing client therefore waits within its existing ensure/startup
deadline for the finite owner to exit, then starts or discovers normally. It
neither borrows the short-lived owner nor replaces it. Deadline expiry reports
`conflict: client_owned` with the remedies: wait for that client to finish and
retry, or use a separate `HERMES_HOME` for parallel clients. The live descriptor
and runtime status identify client-owned mode. Only the ensure invocation
holding the live Popen child and its per-invocation random lease token accepts
that private endpoint through ordinary discovery; arbitrary finite, interactive,
TUI, cron, kanban and ACP clients remain non-attachable. The PID is not identity:
Windows venv redirectors and bootstrap relaunches insert interpreter hops. The
token travels only in the private launch environment, survives those hops, and
is consumed before workers/tools inherit it. It is returned only over same-user
private control identify, never persisted in status or logs.

The owner's managed compute workers and delegated child stores are not arbitrary
clients. They prove their existing durable adoption claim through the private
`worker-identify` control verb: the owner-issued secret is bound to the session,
execution, generation, live same-user PID/birth, profile and authenticated socket
principal. The same verifier used by `worker.adopt` checks the claim, and terminal
assignments are refused. Only this authenticated response bypasses the public
client-owned refusal; the WebSocket adoption still independently rechecks it.
Creator PIDs and same-user socket access alone confer no worker authorization.

`gateway run --client-owned` requires a pipe on stdin. TTY and `/dev/null` fail
before startup with an explicit instruction to omit the flag for a persistent
gateway. This makes the lifetime fence mandatory rather than silently detaching.

A finite runner never performs daemon crash recovery, so it must also leave the
previous daemon's `.clean_shutdown` receipt and restart-failure counters exactly
as found, including when one of its own turns succeeds. Its own graceful exit
is not evidence that the prior daemon drained.
The next daemon still makes the original clean-versus-unclean recovery decision.

The hard lifetime deadline exits **74**, not zero: a wedged forced teardown must
not masquerade as successful graceful shutdown. The creator still blocks while
reaping its exact child. This deliberately adds about **three seconds typical**
exit latency when hard teardown is needed, and up to **ten seconds worst case**
(two five-second waits with an intervening kill) if the early watcher never
started. This no-orphan trade-off is retained; the review changes no reap timing. A spawn
that completes after lease closure performs the same bounded wait/kill/wait.
After a Python SIGTERM handler is installed, EOF requests ordinary shutdown
before registry cleanup so that handler gets the full three-second budget.
Before handler installation, cleanup must precede default SIGTERM (which exits
immediately and would strand detached tools). Windows also retains cleanup-first
because os.kill(SIGTERM) is TerminateProcess even with a Python handler. The
registry is bounded to 0.5 s and the hard-exit timer already runs in both cases.

Review preflight: upstream guidance was rechecked at
`717447e4217ecd9d520f60c6a06c63ec448e601b` (root and subsystem AGENTS,
CONTRIBUTING). Independent proposal: extend existing descriptor/ensure policy
and shutdown receipt ownership, not a second lock or lifecycle manager. Related
open/closed PR searches for client-owned gateway and clean-shutdown recovery,
and issue search for one-shot gateway lifetime, returned no equivalent fix.

Stack fit: reused Python dataclasses, existing runtime ownership/control discovery,
client Popen leases and pytest; added no dependency or parallel lifecycle manager.

## Scope audit

All 25 original changed files remain necessary; the classic-client extension
adds `hermes_cli/gateway_connection.py` and the borrowed-owner Vertex fixture
in `tests/e2e/core/providers/test_native_vertex_tools.py`. The review adds the
real same-home competing-client and daemon-recovery regressions below and
extends the existing status merge schema and preserves the cross-process OAuth
lock fixture (31 files total). No protected gate
files change.

| File | Kept lifetime-bound responsibility |
| --- | --- |
| `gateway/client_lifetime.py` | Require pipe stdin, early EOF watcher, three-second self-exit, exact tracked tool-child cleanup. |
| `gateway/run.py` | Thread client-owned mode into the runner entry point. |
| `gateway/run_bootstrap.py` | Select finite session-only startup, not daemon enrollment. |
| `gateway/run_client_owned.py` | Preserve providers/plugins/MCP while excluding daemon startup. |
| `gateway/run_runtime.py` | Skip bot recovery and READY-hosted daemon coordinators. |
| `gateway/run_shutdown.py` | Refuse restart helpers/replacements that could escape the lease. |
| `gateway/run_startup.py` | Dispatch finite mode before ordinary daemon startup. |
| `gateway/status.py` | Merge explicit client-owned mode into runtime status; reusable daemon startup resets it. |
| `hermes_cli/gateway.py` | Forward the internal finite-owner marker. |
| `hermes_cli/gateway_chat.py` | Pass finite transport-chat intent into the lease path. |
| `hermes_cli/gateway_client.py` | Lease across discovery, attach and every failure. |
| `hermes_cli/gateway_client_lifetime.py` | Close/reap only exact Popen children, including late spawns. |
| `hermes_cli/gateway_connection.py` | Forward classic finite-client intent at the lazy connection dial. |
| `hermes_cli/gateway_runtime.py` | Pass ownership only to a new unmanaged spawn; borrow reusable daemon owners, refuse other finite owners. |
| `hermes_cli/gateway_runtime_start.py` | Private stdin pipe, detached session and closed inherited descriptors. |
| `hermes_cli/gateway_runtime_service.py`, `tests/hermes_cli/test_gateway_runtime_service_roots.py` | Requested-root naming and startup validation stay consistent for named custom roots, including under the native account home. |
| `hermes_cli/main.py` | Arm the parent-death fence before heavyweight gateway imports. |
| `hermes_cli/update_cmd_fleet.py` | Avoid redundant Git-history identity resolution when a fresh finite-client home has no update receipt. |
| `tests/hermes_cli/test_fresh_home_startup_identity.py` | A missing receipt never resolves checkout identity; nonempty receipt reconciliation remains covered by existing update tests. |
| `hermes_cli/subcommands/gateway.py` | Parse the internal client-owned marker. |
| `maintenance/disposable-home-gateway-lifetime.md` | Contract, fork ownership, verification and retirement. |
| `tests/e2e/core/parity/_drive_cli.py` | Client exit alone releases finite gateways; operator stop is failure cleanup. |
| `tests/e2e/core/parity/test_entrypoint_parity.py` | Document client-exit teardown without weakening MCP/tool parity assertions. |
| `tests/e2e/core/providers/test_native_vertex_tools.py` | Explicitly own the persistent serving fixture for unchanged cross-client token-reuse assertions; prove finite clients do not stop that borrowed owner. |
| `tests/e2e/core/providers/test_oauth_shared_pool.py` | Two production-resolver/AIAgent subprocesses still race the same auth.json lock; a common gateway would erase the cross-process property. |
| `tests/gateway/test_client_lifetime.py` | Real EOF and wedged-shutdown bound, including an active tool child. |
| `tests/gateway/test_client_owned_bootstrap.py` | No daemon services/restarts; first-turn MCP readiness retained. |
| `tests/gateway/test_client_owned_recovery.py` | Preserve prior daemon receipts, counters and interrupted turn through finite startup/exit; next daemon takes unclean recovery. |
| `tests/hermes_cli/test_client_owned_discovery.py` | Real bootstrap/control status marks finite ownership; second same-home client waits without attaching, has a bounded timeout remedy, then owns its successor gateway. |
| `gateway/session_worker_discovery.py`, `hermes_cli/gateway_worker_discovery.py` | Private discovery using the existing durable worker-adoption claim. |
| `tests/hermes_cli/test_finite_managed_workers.py` | Real finite safe-mode and delegated-child turns; reject claim tampering and terminal assignments. |
| `tests/hermes_cli/test_gateway_chat.py` | Finite/interactive transport ownership and mock keyword contract. |
| `tests/hermes_cli/test_gateway_client_lease.py` | Exact-child lease, cancellation/failures/borrowing and classic finite dial. |
| `tests/hermes_cli/test_gateway_client_lifetime.py` | Concurrent real normal/SIGTERM/SIGKILL exit for transport and classic clients. |
| `tests/hermes_cli/test_gateway_startup_validation.py` | Assert finite mode takes the correct startup validation boundary. |
| `tests/hermes_cli/test_stream_json.py` | Preserve existing JSONL protocol coverage with the new connect keyword. |
| `tests/cron/test_detached_owner_restart.py` | Complete absent/ready discovery mocks through detached owner retry and receipt replay. |
| `tests/gateway/test_goal_peer_rpc.py`, `tests/gateway/test_bot_dm_held_by_session_writer.py`, `tests/tools/test_bot_live_owner_delivery.py` | Keep all sibling discovery doubles aligned with the real reason-code field. |
| `hermes_cli/_startup_fast.py`, `hermes_cli/gateway_runtime_cli.py`, `hermes_cli/gateway_runtime_discovery.py` | Own argv detection, run options and finite-owner refusal policy without growing capped entrypoints. |

Tracked tool cleanup is not generic process hardening: execution review found
that `os._exit` bypasses ordinary drain-before-kill while foreground tools use
`start_new_session=True`, leaving live tool children after owner death. MCP
readiness preservation addresses the same review's missing finite first-turn
discovery barrier; session-only startup must not regress its only tool snapshot.
Both findings were recorded in execution review of the initial patch.
Regressions are executable in
`test_owner_eof_forces_exit_when_shutdown_signal_is_ignored[True]` and
`test_client_owned_bootstrap_does_not_start_daemon_services` (await-once), plus
the unchanged first-turn MCP/hook and zero-orphan parity assertions.
Hosted qualification of the classic extension exposed the Vertex fixture's
implicit daemon assumption: it expected one process-local token cache across two
finite clients, but correctly observed two mints once their auto-starts ended.
The fixture now explicitly starts/owns its persistent serving daemon for that
scenario. Its one-mint and wire assertions are unchanged, with new assertions
that both finite clients leave the borrowed process alive; teardown uses only
the fixture's exact Popen handle. This is not an unrelated timing-flake rerun.

No whole-file cuts were justified. The inherited docstring narrowing parity to
`-z` was dropped because finite classic clients now have the same contract.

Upstream guidance was rechecked at
`4d26e0cceaed0a08ede833caec0ec6d6ca77a4b8` (root/subsystem AGENTS and
CONTRIBUTING). Open/closed PR searches for gateway one-shot lifetime and issue
searches for gateway idle-exit found no equivalent ownership fence.

## Verification and retirement

Contract tests: `tests/hermes_cli/test_gateway_client_lifetime.py`
Contract tests: `tests/gateway/test_client_owned_bootstrap.py`
Contract tests: `tests/hermes_cli/test_finite_managed_workers.py`
Contract tests: `tests/hermes_cli/test_client_owned_discovery.py`
Contract tests: `tests/gateway/test_client_lifetime.py`
Integration: `tests/e2e/core/parity/test_entrypoint_parity.py` now requires finite
client exit alone to release the owner; operator stop is failure cleanup only.
The existing first-turn MCP/hook parity and zero-MCP-orphan assertions remain.

Run through `scripts/run_tests.sh` with checkout-owned test dependencies and temp
homes only. The subprocess regression drives the real connect/ensure/Popen path,
with a fake provider/gateway boundary, for concurrent normal exit, SIGTERM and
SIGKILL. It asserts process death and canonical runtime absence within seconds.
Existing runtime discovery/service, idle-exit and gateway startup tests preserve
interactive and supervised behavior.

Classic extension RED: before forwarding the finite marker, the real-process
classic case failed after seven seconds with its owners still alive (one failed,
one deselected). Afterward the urgent heavy-slot canonical run passed 311 tests
across 31 files, zero failed, 47 platform skips. That includes both transport and
classic normal/SIGTERM/SIGKILL regressions, exact ownership, daemon isolation,
runtime/discovery/service, idle-exit, launchd, shutdown, connection/reconnect and
classic agent/session-store tests. All eight strict entrypoint parity cases are
Linux `/proc` tests, so hosted Linux qualification must execute them rather than
counting their macOS skips as passes. Hosted run `41407145828` executed all eight
strict parity cases successfully at `75bb6e3cd26cb006ec62d681b3fdc35e1a74d4ae`,
then found the relevant Vertex persistent-fixture assumption above. After the
explicit borrowed-owner fixture change, the urgent heavy-slot follow-up passed
16 tests across three files (zero failed; one existing Vertex default-toolset
xfail), including five Vertex wire tests and both lifetime/lease modules.

Review-fix RED was exercised against the committed candidate before source changes:
second-client discovery attached to the first client, both prior-clean/prior-crash
counter assertions changed, and both forced-EOF cases returned zero instead of 74
(five failures). The recovery fixture was first corrected to seed/read SessionStore
off-loop, so its RED exercises the counter invariant rather than the loop guard.
Additional RED cases catch creator-PID loss through multiplexer discovery and
successful finite turns clearing a prior daemon counter.

Review-fix GREEN: the canonical urgent heavy-slot runner with `--file-retries 0`
passed **474 tests across 54 files**, **zero failures**, 62 platform skips and
one existing Vertex xfail. Coverage includes lifetime/bootstrap/discovery/recovery,
client/runtime/service, idle-exit, launchd drain caps, shutdown/status, UTF-8
subprocess contracts, Vertex wire behavior and parity collection. The eight strict
entrypoint parity cases remain Linux-only; their execution is a hosted gate duty,
not a claimed macOS pass. The review delta code-health check against
`2996d712414d820a88333a781907eab7378e7325` has zero blockers/advisories.
`git diff --check` and the source-only Fork-Patch ownership check pass.

The first hosted review-fix run (`41492463206`, head `32a3f147c49d`) passed
static/Node, eight Python shards and strict parity (eight cases), but exposed the
OAuth fixture's reliance on concurrent same-home finite clients. That fixture now
launches two production provider-resolver/AIAgent child processes against the same
auth store (distinct PIDs asserted), preserving every refresh/401/wire assertion
without a shared gateway or parallel profile-store semantics. The canonical
runner passed its Linux/container execution (one test, zero failures) and its
delta code-health check has no findings. The separate image-catalog assertion
in shard 7 is unrelated and was not treated as a rerunnable timing flake.

The worker-authorization/bounded-wait follow-up was RED against `66342d1c`:
both real finite CLI variants (`-z --safe-mode` and a managed turn that delegates)
reached the 65-second subprocess deadline; the competing finite client failed
immediately; and TTY/null stdin failed the expected early explanatory refusal.
The source archive's failure-cleanup stop command also returned nonzero after
the CLI timeouts; the original `TimeoutExpired` exceptions remain in its log.

Follow-up GREEN: the urgent heavy-slot canonical runner (`--file-retries 0`)
passed **1706 tests across 171 files**, zero failures, 199 platform skips. It
includes the managed-worker and runtime-session-store suite, full core provider
and parity collections, lifetime/bootstrap/discovery/recovery, client/runtime,
idle/launchd/shutdown, and UTF-8 contracts. Strict Linux/Windows skips still
require hosted execution. After moving the unchanged legacy control identity
closure into `gateway/run_control_identity.py` to respect the facade cap, the
11-file control/bootstrap/ensure/CLI follow-up passed **109 tests**, zero failures,
23 skips. After factoring the shared retirement/deadline decision into
`hermes_cli/gateway_runtime_start.py`, the final six-file ensure/CLI/UTF-8 run
passed **835 tests**, zero failures, 11 skips, and the delta health check against
`66342d1c` has **zero blockers and zero advisories**. The four old WorkerRPC
fixtures now include the real `GatewayDiscovery.reason_code` field; production
uses the typed field directly, without a compatibility fallback for incomplete
mocks. No assertions or health gates were weakened.

Hosted run `41553886065` at `cf6d517b` exposed two remaining discovery doubles
in `tests/cron/test_detached_owner_restart.py` that omitted `reason_code`.
Both absent/ready cases and all sibling `SimpleNamespace` discovery doubles
across `tests/` now provide `reason_code=None`, including goal-peer and bot
fixtures. The original cron file reproduces six failures before the correction.
Production's typed field access is unchanged.

Session-end triage ran the complete `test_session_end_delegations.py` file five
times each at original head `cf6d517b63941f7883df353914503f3c80833e12` and fresh
base `bb638f20d6383e3d59c4b47130115064a7977a73`: all ten invocations passed all
13 tests with retries disabled. The hosted failure occurs during the fixture's
initial SessionStore database open (zero-byte quarantine/schema race), before
any finite-client bootstrap or worker discovery. Its test, SessionStore,
SessionDB, fake ownership initializer, delegation implementation and test
teardown are identical on base and head. No session-end change is justified by
this evidence; the observed database-creation flake remains separate from the
reproduced discovery-mock regression.

Rebase qualification also hit code-health caps tightened on fresh main. The
added lifetime argv probe, config/lifecycle/READY enrollment policy, owner refusal
and unmanaged-spawn options now live in their existing topical siblings rather
than growing already oversized entrypoints. This is a responsibility extraction;
no cap, protected gate file or assertion was changed.

The full local cron/gateway/agent plus prior-target run exercised 3,201 files:
35,087 passed, 46 failed. Its default developer environment lacked messaging and
hindsight extras. Rerunning all 12 failed files with the hosted CI extra matrix
cleared dependency failures and transient cron/outbox failures. Exactly three
failures remained, identically on head and clean base `bb638f20`: both
`test_async_token_accounting.py::TestDurability` garbage-collection tests and the
`test_session_listener_lazy_dashboard.py` dashboard HTTP timeout. These are
recorded base failures, not a claimed all-green local suite. The 177-file
prior-target/entrypoint verification passed 1,851 tests in 177 files after the
final rebase to `002f8807dbdef3b9aa450980baa501c6e9920f58`, with retries disabled.
Delta code health reports zero blocking findings (one existing early-watchdog
thread advisory), source-only Fork-Patch reports zero problems, and diff check
passes.

Round-three startup profiling ruled out a lease handshake/backoff and an extra
interpreter hop. In eight-child paired traces, spawn/ensure itself took under
0.4 seconds before polling the child. The pre-readiness hotspot was instead
`_recover_update_debts_on_startup -> _receipt_reports_stale_runtime({}) ->
_current_checkout_sha -> get_code_identity(refresh=True)`: even an empty receipt
walked source Git history in both the finite client and the gateway. Instrumented
runs spent 9.7 seconds there on base versus 14.8–15.2 seconds on head, including
multiple three-second Git-probe deadlines. Import timing showed no corresponding
multi-second added lifetime import. A missing receipt now returns before identity
resolution; real nonempty receipt/restart reconciliation is unchanged. The new
regression asserted zero identity resolutions, failed before the fix (one call),
and passed afterward alongside immutable-release and external-runtime coverage.
Paired canonical catalog runs passed all 39 children in both lanes: base
`eed51855` starting/running/total medians were **19.066/19.292/24.198 s**;
head after the fix was **10.402/10.833/15.869 s**. Post-final reap medians were
0.076 s versus 0.354 s, confirming the retained teardown bound is not the
startup regression. Head left **zero** live children versus **37** on base;
only those exact recorded base fixture processes were cleaned after census.

The 183-file follow-up exposed a test-environment mismatch: the named-root
fixture patched `Path.home()` but retained the real OS account home. When
scratch itself lived below that account's Hermes home, installer and runtime
therefore used different native roots. Pin the fixture's simulated account
alongside `Path.home()` rather than changing production service identities.
The installer-consistency regression was RED when a nested custom profile
incorrectly got `worker`; restoring the native-root containment rule makes it
use the installer's path hash. The contract also covers a direct named profile
and custom roots outside the native home, comparing both launchd and systemd
names without patching `Path.home()`. The original broad-case failure is covered
with the corrected fixture. Client-lifetime arming follows the bootstrap path
insert and still precedes startup and other heavy imports.

Review-correction verification (checkout-local Python 3.14.7; all test entry
points pin scratch HOME/HERMES_HOME/HERMES_RUNTIME_DIR and use urgent heavy-slot):
new consistency contract RED on `e2dba09a` (`worker` versus installer hash), then
7 passed/1 platform skip after the fix. Restored production naming separately
reproduced the old named-fixture failure before the account-home fixture fix.
The unchanged 183-file selection is GREEN: **1,879 passed, 0 failed, 200 platform
skips**, retries disabled, 510.2 s. A script-mode bootstrap-prefix probe is RED
before moving lifetime arming and GREEN afterward; ordinary script `--version`
also succeeds. Delta health has zero blockers and the existing early-watchdog
thread advisory; source-only Fork-Patch and whitespace checks pass.

One repeated paired canonical catalog timing passed **39/39 children in each
lane**. Starting/running/total medians are **18.656/18.923/23.908 s** on baseline
`eed51855`, **17.900/18.932/25.848 s** on this correction. The full file took
128.2 s versus 122.6 s. This loaded-machine repeat does not reproduce the earlier
8.7-second startup-median improvement; starting is slightly earlier but running
is effectively level and total median is slower, so do not claim that magnitude
from this run. Fresh-receipt identity suppression remains independently GREEN.
Head again leaves **zero** live children; base leaves **27**, all exact recorded
fixture children cleaned afterward. Final-output-to-exit medians are 0.127/0.404 s.

Retire when released upstream bounds finite-client auto-starts including killed
clients and prevents those owners from running unrelated daemon services.
Rollback by reverting commits carrying the patch identity; no persistent config
or state migration is involved. No live runtime rollout is part of this patch.
