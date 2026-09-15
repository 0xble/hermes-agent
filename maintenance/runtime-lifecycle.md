# Runtime lifecycle and restart ownership

This responsibility covers source updater, restart inbox, process deadlines, supervisor/profile ownership and recovery notices. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Coupled current adaptations

Restart-inbox replay checks running/non-draining admission before reconciliation,
before each profile claim, and before each dispatch. Shutdown returns unprocessed
exact claims to pending through the existing CAS and refunds their attempt budget.
The claimed-event ingress guard also covers adapter background-task handoff and
awaited admission, marking rejection before refund so no-agent completion cannot
incorrectly mark the input delivered. Already-started execution remains untouched.
Real SQLite and adapter regressions in `tests/gateway/test_restart_inbox.py` cover
both shutdown flags and restore claimability after admission reopens.

Update notification deadlines retain native update admission while an explicit
`fleet_restart_pending` obligation remains, even when the wrapper has exited.
The timeout acknowledgement survives runner restart without duplicate delivery,
and later verified fleet completion sends the final notice once and releases
admission. Explicit failed-wrapper outcomes and the existing completed-wrapper
without-usable-receipt timeout contract remain unchanged. The real watcher,
persisted receipt and launch-admission regressions live in
`tests/gateway/test_update_timeout_admission.py`.

The process-handoff regression now holds its real child on an explicit socket
release until ownership transfer and sibling accounting finish. The former
0.4-second lifetime could expire before handoff under suite load, correctly
triggering the production refusal. A causal reproduction confirms that path,
although the original failing run did not preserve the returned error object.
The fixture then awaits completion publication before draining the parent notice.
This changes no production handoff semantics.

Cold-start reconnect tests now await the owned reconnect task before asserting
its bookkeeping slot is gone. Adapter publication intentionally precedes awaited
recovery work and final cleanup. An event barrier in the real recovery await
reproduces that intermediate state deterministically and joins task cleanup even
on assertion failure. This test-only correction changes no reconnect behavior.

The background-deadline test fixture joins its reader and deadline threads before
releasing the shared checkpoint-path override. A controlled reproduction showed
an exited reader outliving the old fixture and overwriting the next checkpoint,
causing recovery to return zero. The regression uses an explicit teardown/join
handshake and a real checkpoint recovery. Production process handling is unchanged.

Origin-only updater branch switches select the existing remote fallback before
the compatibility guard, install its checked immutable SHA, and configure tracking
after checkout. Existing incompatible branches and tags remain refused. Real Git updater regressions cover this correction without changing runtime configuration.

## Agent-requested native update lifecycle — source candidate

- **Pinned gateway handoff (September 14):** `hermes gateway update --revision SHA --reason ...` carries the exact lowercase40 target through a fail-closed `agent-update-revision` verb, pending metadata and POSIX/Windows native launch into the existing pinned updater. No checkout of stale local `main`, guard bypass, snapshot/cron/ledger changes or automatic unpinned fallback. A legacy running gateway needs operator-owned pinned bootstrap, not a newer client silently dropping the pin. Duplicate admission is refusal, acceptance is not completion, and gateway CLI failures retain their process exit codes.
- **Pinned handoff provenance/retirement:** local adaptation of the existing fork-native agent admission and immutable-revision contract; upstream `main` guidance inspected at `1ab32b212b3828be8239bd68ac3687756bf7c5c2`. Topic searches found no equivalent released handoff fix; open PR107445 remains the adopted launcher ancestor, not a claim of upstream acceptance. Retire only when released upstream preserves the complete pinned handoff/compatibility contract. Revert the scoped source commit only after resolving pending requests; never rewrite historical receipts or result/ledger state. Native candidate review and parent-owned landing/activation remain separate gates.
- **Pinned handoff proof:** `tests/gateway/test_update_revision_handoff.py` exercises a real CLI process, native control socket registration, SQLite lineage, detached POSIX child and production guarded Git runner against a temporary local remote. Compatible target succeeds from compatible detached HEAD with stale incompatible local main unchanged; incompatible targets are refused. Malformed pins, old gateways and duplicate requests cannot launch an unpinned updater or report success. Test fixtures never run dependency installation or live service restart; Windows argv threading is source-covered, not claimed as live Windows verification.
- **Contract:** [agent update lifecycle](../website/docs/developer-guide/agent-update-lifecycle.md): required short reason, durable direct/nested parent-topic resolution, shared native `/update` launch and progress, separate Updating/Restarting/final notices, honest recovery attempts and receipt-plus-runtime completion evidence. No synthetic chat events, inferred reasons, second restart, or extra scheduler.
- **Drain:** native active-work accounting includes background delegations; the initiating turn hands off immediately rather than waiting on its own drain.
- **Upstream:** adopts open PR [107445](https://github.com/NousResearch/hermes-agent/pull/107445), issue [107427](https://github.com/NousResearch/hermes-agent/issues/107427), preserving its author. The remaining local delta adds agent routing, durable reason/progress and stricter finalization evidence. Retire adopted/private portions when released upstream satisfies the complete contract.
- **Verification:** real Unix socket and SQLite lineage; native receipt persistence, restart simulation, send failures, legacy/no-reason records, duplicate admission and drain regressions. Source tests are not live rollout evidence.
- **Crash-safe admission:** `gateway/update_launcher.py` uses the existing OS file-lock primitive and a separate, nonauthoritative staging file. Only the current lock owner initializes output and atomically publishes complete JSON. Abandoned staging is reusable; pending/claimed records (including legacy unreadable records and uncertain spawn outcomes) remain fenced. Never unlink the stable admission lock or reclaim by age. `tests/gateway/test_update_admission_crash.py` covers real isolated initializer death, live contention, torn staging, claimed races and unresolved publication.
- **Claimed-marker history:** Current notification writers no longer perform pending-to-claimed transfer, removed by `d58a81299e7`. Claimed paths remain authoritative legacy input. A historical rename simulation does not establish a current concurrent writer; future transfers must define synchronization with checkpoint saves.
- **Known launch refusal:** Only native process creation can classify executable-not-found or permission-denied as no child started. That exception releases the same pending inode and bytes only while no claimed marker exists. Arbitrary spawn exceptions retain the fence. Preserve this distinction when adopting upstream launcher changes. Reverting this correction restores manual recovery for definite launch failures, never permission to clear ambiguous requests.
- **Timeout admission:** notification expiry acknowledges only the notice while updater termination is unknown; retain the request, output and wrapper files until canonical completion/reconciliation. `tests/gateway/test_update_timeout_admission.py` covers refused relaunch, pending/claimed ownership, restart deduplication, send failures and later verified/failed/unverified completion. Roll back this correction only with its scoped commit; older code does not honor the new `timeout_notified` checkpoint, so never downgrade while an unresolved update is pending.
- **Activation boundary:** authorized rollout is default local only, coordinated after checking other work. Product keeps native profile/fleet semantics. No remote or other-profile deployment is authorized by this entry.

- Proven executor non-admission records exact input-owner/turn-token evidence before releasing a restart-inbox claim. A coalesced capacity callback retries through existing drain/reconciliation, refunding the rejected attempt. Ambiguous execution remains parked. Regression: `tests/gateway/test_restart_executor_recovery.py`. Rollback preserves restart links and must not treat the additive `not_started` proof as ingestion.

- Process checkpoint uncertainty and unreadable-source write protection belong to each recovered checkpoint path. Recovering a healthy secondary profile cannot clear another home's fence or copy its malformed records. `tests/tools/test_delegation_process_checkpoint.py` exercises two real homes and explicit source repair. This extends the existing registry, with no new recovery loop.

- Preserve updater completion verification through upstream dependency-repair decomposition; all updater exercises in this source run use isolated test doubles, not installed-runtime updates.

- Adopt upstream's `_interrupt_running_turn` sync core and `_drop_turn_slot` guarded release. The fork's /stop contracts — generation-owned continuation and restart-turn stop-owner cancellation — are re-expressed in the async `_interrupt_and_clear_session` wrapper, not duplicated in the sync core.

- `gateway.multiplex_profile_allowlist` is retired in favour of upstream's config migration 42 to 43. The fork keeps only its own `restart_resume_policy` normalization in that hook.

- Preserve private-fork update discovery through authenticated noninteractive `ls-remote`, adopting upstream's no-fetch passive checks and HEAD-aware cache while retaining source invalidation. A private API 404 must not redirect checks to public upstream or fabricate a count.

- Restart inbox ownership is linked to the exact normalized event and its canonical input owner. Reconciliation chooses original replay or transcript continuation before either consumer runs. A required input must be durably present before native or Codex model dispatch. Unknown ingestion and previously attempted controls without proof remain parked. Routing JSON fallback is insufficient admission evidence. This does not promise exactly-once arbitrary external effects.

- Named-profile update requests already retain profile identity in their session key. Both CLI socket discovery and gateway database resolution must use the actual owning profile, including the default multiplexer. Missing, unreadable or ambiguous ownership rejects rather than guessing a payload-supplied path.

- Each supervised updater gets a unique systemd scope unit.

- The pinned-revision readback requires a live row for every gateway in the pre-update inventory and rejects an empty probe without one.

- Live-owner checkpoint retry treats every non-live status as terminal, so `budget_exhausted` and `interrupted` deliver.

- An errored deadline kill keeps the process session running with bounded retries instead of marking it exited.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-003 | Retired |
| HERMES-012 | Retired |
| HERMES-030 | Retired |
| HERMES-041 | Retired |
| HERMES-043 | Retired |
| HERMES-058 | Active |
| HERMES-061 | Retired |
| HERMES-075 | Active |
| HERMES-093 | Active |
| HERMES-097 | Active |
| HERMES-099 | Active |
| HERMES-101 | Active |
| HERMES-105 | Active |
| HERMES-114 | Active |
| HERMES-115 | Active |
| HERMES-123 | Active |
| HERMES-134 | Active |
| HERMES-138 | Active |
| HERMES-072 | Active |
| HERMES-088 | Active |

## Patch records

### HERMES-003 — Retired fixed file-descriptor soft-limit floor

- **Summary:** The private pre-dispatch helper that best-effort raised `RLIMIT_NOFILE` to a fixed 8192 has been removed. Upstream now owns the complete contract through profile-aware `runtime.nofile_soft_limit`, a shared `apply_nofile_soft_limit()` helper for gateway and dashboard/serve entrypoints, and matching generated-service limits. The upstream implementation preserves the private safety properties: POSIX-only, best-effort, never lowers an existing limit, and clamps to a finite hard limit.
- **Surfaces:** Historical private surfaces were `hermes_cli/main.py` and `tests/test_fd_soft_limit.py`. The active replacement is upstream `hermes_cli/resource_limits.py`, its gateway/dashboard call sites, configuration, service generators, and upstream tests.
- **Upstream tracking:** Replaced by released upstream commits `87aedbe7b`, `0472c31aa`, `373631bea`, and `acb7547da` (including the configurable process and service-manager limit contract). Related historical issues were `#36899` and `#75269`.
- **Upstream PR:** None; replacement landed as released commits rather than a tracked PR in this record (checked 2026-08-14).
- **Regression:** Upstream resource-limit tests plus the repository's canonical suite. Runtime supervisor acceptance remains part of a separately authorized deployment, not fork synchronization.
- **Rollback:** Do not restore the private helper or test. If the upstream replacement regresses, fix or backport the upstream `runtime.nofile_soft_limit` path as one coherent contract; do not layer a second pre-dispatch limit implementation over it.
- **Additional historical subjects (optional provenance):** `chore(local): carry Brian-owned working-tree patches into the fork`; `docs(fork): retire fd soft-limit patch`.

### HERMES-012 — Retired GitHub Actions fork synchronization pipeline

- **Summary:** The former three-workflow GitHub Actions pipeline remains retired. The external control plane owns scheduling. Dedicated source reconciliation preserves its retained worktree and frozen upstream boundary without requiring runtime deployment.
- **Surfaces:** Historical subjects in the index, this record, and the repository patch validator. Scheduler state and accepted execution receipts are external.
- **Upstream tracking:** Fork-owner release machinery, not an upstream product defect.
- **Upstream PR:** None. No upstream contribution is proposed.
- **Regression:** Prove the retired GitHub synchronization workflows remain absent. Source reconciliation must preserve dirty work, verify the exact integrated candidate, enforce independent review and remote-head fencing, and read back publication without runtime mutation.
- **Rollback:** Do not restore the retired workflows. Preserve incomplete source work and repair the existing source owner. Runtime promotion remains separately authorized.
- **Additional historical subjects (optional provenance):** `chore: automate maintained fork synchronization`; `fix: use fork-safe candidate verification`; `fix: promote only dispatched fork candidates`; `chore(fork): enforce maintained patch manifest`; `chore(fork): adopt root maintenance manifest`; `chore(fork): retire GitHub sync workflows`; `docs(fork): retire plugins superseded upstream`; `fix(sync): reconcile fork patches with current upstream APIs`; `fix(fork): remove duplicate reconciled toolset entry`; `docs(fork): reconcile maintenance ownership and patch registry`; `maintain-targets`.

### HERMES-030 — Preserve the launchd supervisor marker across the stderr wrapper

- **Retired (2026-08-18):** Released upstream commit `c69a0872ea` now preserves supervision at the actual wrapper boundary: `stderr_timestamp` detects a real nonzero launchd `XPC_SERVICE_NAME` on itself and passes `HERMES_GATEWAY_EXTERNAL_SUPERVISOR=1` only to its gateway child. Interactive `XPC_SERVICE_NAME=0` children remain unmarked. The private generated-plist environment entry and its duplicate tests were removed; upstream's wrapper tests now own the contract. Runtime regeneration and a live launchd canary remain part of a separately authorized promotion and were not performed during source reconciliation.
- **Observed failure (2026-08-15):** Promoting the fork sync (`0.20.0` → `0.20.1`, runtime repo at `401d808610`) ran `hermes gateway start`, which regenerated `ai.hermes.gateway.plist`. Upstream `1db9273584 fix(gateway): timestamp launchd error log lines` had changed `ProgramArguments` from a direct `python -m hermes_cli.main gateway run --replace` exec to `python -m hermes_cli.stderr_timestamp --error-log … -- python -m hermes_cli.main gateway run --replace`. From that rewrite the runtime gateway never started again: every spawn printed `A gateway is already running under launchd for this profile.` and exited 1, and `KeepAlive` respawned it every `ThrottleInterval` (30s) indefinitely, so Hermes was fully down until the marker was restored. Measured on the live job: the gateway process reported `XPC_SERVICE_NAME="0"` with its ppid being the wrapper.
- **Root cause:** launchd stamps `XPC_SERVICE_NAME` with the job label only onto the process it spawns **directly**. With the wrapper in `ProgramArguments` that process is `stderr_timestamp`, and macOS resets the `Popen`-ed grandchild's `XPC_SERVICE_NAME` to the sentinel `"0"` rather than inheriting the label (macOS actively manages this variable — a process falsely claiming a service name is `SIGABRT`ed). `is_gateway_supervisor_process()` treats `"0"` as "an interactive shell launched me", so `_guard_supervised_gateway_conflict()` sees `service_installed=True, service_running=True` from launchd's own registration, concludes a *different* gateway owns the profile, and `sys.exit(1)`s on the service's own startup — the exact respawn/refuse loop its docstring warns against.
- **Summary:** The historical private implementation declared `HERMES_GATEWAY_EXTERNAL_SUPERVISOR=1` in generated launchd plists. Released upstream now owns the same safety contract more narrowly in `hermes_cli/stderr_timestamp.py`, forwarding the marker only when the wrapper itself has a real launchd service label.
- **Surfaces:** Historical private surfaces were `hermes_cli/gateway.py` and two cases in `tests/hermes_cli/test_gateway_service.py`. The active replacement is upstream `hermes_cli/stderr_timestamp.py` with `tests/hermes_cli/test_stderr_timestamp.py`.
- **Upstream tracking:** Replaced by released upstream commit `c69a0872ea` (contained in tags `v2026.8.16`, `v2026.8.16.2`, and `v2026.8.18`), which closed issue #86893 on 2026-08-16 after four independent macOS reproductions.
- **Upstream PR:** None; the released replacement is tracked as commit `c69a0872ea` and issue #86893 (checked 2026-08-18).
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_stderr_timestamp.py tests/hermes_cli/test_gateway_service.py -q`. Runtime promotion separately regenerates the plist and proves one live launchd start without a respawn loop.
- **Rollback:** Do not restore the private generated-plist marker or duplicate tests. If wrapper propagation regresses, repair or backport upstream's `_child_env_for_command()` boundary while preserving the rule that interactive `XPC_SERVICE_NAME=0` processes are never marked supervised.
- **Additional historical subjects (optional provenance):** `fix(launchd): preserve supervisor marker through gateway wrapper`; `docs(fork): register HERMES-030 launchd supervisor marker`.

### HERMES-041 — Retired gateway lifecycle self-control hardening

- **Retired (2026-08-27):** Brian directed full retirement in favor of exact upstream behavior. Removed: the inherited `_HERMES_GATEWAY` marker taint on `hermes gateway restart` (upstream's `_is_supervised_gateway_process` probe restored), the destructive-launchctl verb classifier, wrapper/option/eval/env bypass closure, dynamic-execution carrier inspection, and the fork-only `tests/cron/test_gateway_lifecycle_guard_launchctl.py`. `hermes_cli/gateway.py` and `cron/lifecycle_guard.py` were restored to the fork's upstream base (`7a1aafb4e1`); upstream's logical-line parsing, transparent-wrapper traversal, profile targeting, and data-operand exemptions remain the active contract. Removal commit: `refactor(safety): retire fork lifecycle guard patches`.
- **Surfaces:** Historical: `hermes_cli/gateway.py`; `cron/lifecycle_guard.py`; `tools/terminal_tool.py`; `tests/cron/test_gateway_lifecycle_guard_launchctl.py`; `tests/hermes_cli/test_gateway_restart_loop.py`.
- **Upstream tracking:** Retirement was a deliberate policy decision; upstream's restart guard passes gateway-derived child sessions that the fork's marker taint blocked (the 2026-08-26 audit had flagged the fork guard's own restart/stop asymmetry).
- **Upstream PR:** None (checked 2026-08-27).
- **Regression:** Upstream-owned `scripts/run_tests.sh tests/hermes_cli/test_gateway_restart_loop.py tests/hermes_cli/test_gateway_service.py -q` against the restored files.
- **Rollback:** Restore the historical stable subjects' file states from fork history in one follow-up commit; the marker-taint and verb-classifier pieces must return together or the guard is inconsistent.
- **Additional historical subjects (optional provenance):** `fix(gateway): fail closed on inherited restart marker`; `fix(safety): block destructive gateway launchctl verbs`; `fix(safety): narrow live lifecycle guard to executable actions`; `fix(safety): close executable wrapper bypasses`; `fix(safety): block dynamic lifecycle executables`; `fix(safety): close wrapper option and eval bypasses`; `fix(safety): inspect env split-string payloads`; `fix(safety): classify wrapped dynamic executables`; `fix(safety): combine argv and command-shape guards`; `fix(safety): keep unresolved script references fail-closed`; `fix(safety): remove dynamic runner and emitter exemptions`; `fix(safety): reject unresolved execution payloads`; `fix(safety): reject dynamic lifecycle control arguments`; `fix(safety): consume nice command separators`; `fix(safety): scope unresolved read-only commands`; `fix(safety): parse env argv0 options`; `fix(safety): inspect dynamic execution carriers`; `fix(safety): reconcile lifecycle guard contracts`; `fix(review): preserve reconciliation safety contracts`.

### HERMES-043 — Retired completed-legacy resume suppression

- **Retired (2026-08-27, record correction):** The completed-transcript suppression added on 2026-08-11 was deliberately removed on 2026-08-15 by HERMES-027 (`fix(gateway): recover unacknowledged terminal responses`), whose contract treats a persisted terminal transcript as model-completion evidence rather than delivery evidence and keeps recent sessions recovery-eligible until outbound delivery is durably acknowledged. The two records had claimed mutually exclusive contracts on the same surface and test file; HERMES-027 is the live contract, and `tests/gateway/test_clean_shutdown_marker.py` asserts recovery eligibility.
- **Surfaces:** Historical surface was `gateway/session.py` `suspend_recently_active`; the file's coverage now belongs to HERMES-027.
- **Upstream tracking:** Not applicable; superseded internally by HERMES-027 before any upstream association existed.
- **Upstream PR:** None.
- **Regression:** Covered by HERMES-027's `pytest -q tests/gateway/test_clean_shutdown_marker.py`.
- **Rollback:** Do not restore the suppression; if resuming completed legacy sessions becomes a problem again, solve it inside HERMES-027's delivery-acknowledgement model rather than re-adding transcript-based suppression.
- **Additional historical subjects (optional provenance):** `fix(gateway): skip completed legacy resumes`.

### HERMES-058 — Make the gateway lifecycle guard configurable

- **Hypothesis:** The current lifecycle safety policy is hard-coded independently in the gateway CLI, terminal tool, and cron creation path. A single default-on `security.gateway_lifecycle_guard` resolver, read live and fail-closed, can preserve existing behavior while permitting an explicit operator opt-out consistently across all three paths.
- **Counter-hypothesis:** The native `/restart` command is sufficient and self-targeting terminal or cron lifecycle commands should remain unconditionally blocked. That remains the safer default, but it does not cover authorized agent-managed activation workflows that deliberately accept supervisor-loop risk and require the same policy at every enforcement boundary.
- **Summary:** Adds a default-on config gate for supervised self-stop/restart/uninstall checks, terminal lifecycle-command inspection, and cron lifecycle-payload validation. Missing, malformed, or unreadable config keeps the guard enabled.
- **Surfaces:** `cron/lifecycle_guard.py`; `tools/terminal_tool.py`; `hermes_cli/gateway.py`; `hermes_cli/config_defaults.py`; configuration documentation; focused gateway lifecycle tests.
- **Upstream tracking:** Issue #30719 introduced the hard guard and mentioned an unimplemented `--allow-lifecycle` override. Closed PRs #35815 and #37057 proposed loop detection or guard removal; open PR #37063 proposes removal rather than a config switch. No released configurable equivalent identified after checked 2026-08-27.
- **Upstream PR:** Related: #35815 (closed), #37057 (closed), #37063 (open); no direct PR after checked 2026-08-27.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_gateway_restart_loop.py -q`; config-default and documentation validation; broader local CI before publication.
- **Rollback:** Set `security.gateway_lifecycle_guard: true` before reverting `feat(gateway): make lifecycle guard configurable`; restart externally and rerun the focused lifecycle tests. Never revert while relying on an in-process restart command for activation.
- **Retirement:** Retire after released upstream provides a default-on, fail-closed operator opt-out that governs the CLI, terminal, and cron paths consistently and passes the fork regressions.

### HERMES-061 — Compare desktop launcher shebang paths case-insensitively (Retired)

- **Summary:** Normalize both sides of the shebang containment comparison to lowercase. This preserves Linux behavior and makes the helper's path comparison consistent on case-insensitive filesystems.
- **Surfaces:** `hermes_cli/linux_desktop_entry.py`; `tests/hermes_cli/test_linux_desktop_entry.py`; this record.
- **Upstream tracking:** Upstream `main` at `93de1d3430a1cb955ef85715cf1f59581295fa21` lowercases only the shebang. A repository issue search found no direct report or fix. Checked 2026-08-28.
- **Upstream PR:** None found as of 2026-08-28.
- **Regression:** `.venv/bin/python -m pytest tests/hermes_cli/test_linux_desktop_entry.py -q`; `test_exec_leaves_venv_shebang_scripts_alone` failed before the fix on macOS and passes after both paths use the same case normalization.
- **Published commit identity:** Stable subject `fix(desktop): compare shebang paths case-insensitively`; source, regression, and manifest record ship together.
- **Rollback:** Revert the one-line normalization and this record. No data migration or persistent state is involved.
- **Retirement:** Retire after a released upstream version performs a case-insensitive comparison for this helper or removes the string-containment test in favor of a filesystem-aware equivalent, with matching regression coverage.
- **Source references from initial investigation:** `/Users/...`; `/users/...`.
- **Additional historical subjects (optional provenance):** `_shebang_escapes_running_env`.

### HERMES-075 — Configure restart continuation policy

- **Independent hypothesis (2026-08-29):** Restart recovery currently overloads `BasePlatformAdapter.interactive_resume` with two independent meanings: whether a platform has a human reply channel and whether an empty startup recovery turn should ask or continue. This prevents an operator from selecting automatic continuation globally or per platform without misclassifying an interactive adapter such as Telegram as non-interactive. The correction belongs in gateway policy resolution, above adapter capability defaults and below explicit per-platform configuration.
- **Loader repair hypothesis (2026-09-06):** The real YAML startup loader flattens a selected key registry and omits `restart_resume_policy`, so the configured continuation policy becomes `None` and interactive recovery selects `ask`. Register the existing key with presence-based precedence. Exercise temporary-home YAML loading through policy resolution and recovery-note construction, plus invalid-value rejection. No new policy, state, adapter capability, or replay behavior is required.
- **Loader repair subject:** `fix(gateway): preserve restart policy through YAML startup (#68)`; surfaces `gateway/config_loader.py` and `tests/gateway/test_restart_resume_policy.py`. Roll back this repair by reverting only that commit, leaving the existing HERMES-075 policy implementation intact.
- **Summary:** Add an opt-in global `gateway.restart_resume_policy` (`ask` or `continue`), allow `gateway.platforms.<name>.extra.restart_resume_policy` to override it, preserve non-interactive adapters' safe continue-only behavior, and generate platform-neutral continuation guidance. The upstream default remains unchanged when no policy is configured.
- **Surfaces:** `gateway/config.py`; `gateway/run_turn_runner.py`; `gateway/run_shutdown.py`; `tests/gateway/test_restart_resume_policy.py`; `tests/gateway/test_restart_resume_pending.py`; `website/docs/user-guide/messaging/index.md`; this record.
- **Upstream tracking:** Open issue #9673 requests automatic continuation without a new user message. Existing upstream restart recovery and `interactive_resume` capability provide session preservation, startup scheduling, freshness, authorization, duplicate-run, restart-loop, and replay safeguards but no operator-selectable interactive-platform continuation policy.
- **Upstream PR:** None found for a global restart-continuation policy with platform overrides as of 2026-08-29. Closed PR #9328 proposed a different global-recent-transcript design; merged recovery work and PR #65783 preserve ask-first behavior for interactive adapters.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_restart_resume_policy.py tests/gateway/test_restart_resume_pending.py -q`; focused coverage must prove platform override > global policy > adapter default, non-interactive safety, platform-neutral continue guidance, validation, and the startup recovery path.
- **Expected published commit identity:** Stable subject `feat(gateway): configure restart continuation policy`; source, focused regressions, docs, and this record ship together.
- **Rollback:** Revert only `feat(gateway): configure restart continuation policy`, remove `gateway.restart_resume_policy` and per-platform overrides from configuration, restore `interactive_resume`-only guidance selection, and remove the HERMES-075 tests and documentation. No schema or persistent-data rollback is required.
- **Retirement:** Retire after a released upstream version provides a documented global ask/continue restart-recovery policy with per-platform overrides, safe adapter capability handling, automatic continuation without a new user message, and equivalent replay/freshness/authorization/loop regressions.

### HERMES-093 — Keep restart notices scoped and accurate

- **Summary:** After a successful Telegram DM-topic notice, suppress only the unthreaded home-channel broadcast to the same private parent chat; forum/group parents, explicit home topics, and distinct home chats remain notified. Mark both active-session and home-channel advisories as interim sends so they cannot seal a live stream. Resolve the configured restart continuation policy per adapter: `continue` says Hermes will try to resume automatically, while `ask` retains the existing instruction to send a message.
- **Surfaces:** `gateway/run.py`; `tests/gateway/test_restart_resume_pending.py`; `tests/gateway/test_restart_notification.py`; this record.
- **Upstream tracking:** No released upstream implementation satisfies the complete contract as of 2026-09-01. Open PR #98445 independently fixes the interim-send contract and is backported exactly for that portion. Open PR #57164 suppresses only idle external-shutdown broadcasts. Open PR #71181 rate-limits repeated process-level broadcasts. None suppresses the redundant unthreaded parent broadcast or makes wording continuation-policy-aware.
- **Upstream PR:** Open PR #98445 is the exact source for the interim-marker portion. No upstream PR covers the complete HERMES-093 contract before publication.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_restart_resume_pending.py tests/gateway/test_restart_notification.py tests/gateway/test_gateway_shutdown.py tests/gateway/test_interim_send_lanes.py tests/gateway/test_stream_final_contract.py -q`; focused cases must prove parent-scope suppression after a same-chat Telegram DM-topic notice, preservation of forum/group parent and explicit home-topic delivery, interim metadata on both send classes, automatic wording under `continue`, and the existing prompt under `ask`. Before implementation, the focused tests fail with two sends instead of one, absent `_interim_send`, and the stale manual-resume instruction.
- **Expected published commit identity:** Stable subject `fix(gateway): keep restart notices scoped and accurate`; source, regressions, and this record ship together.
- **Rollback:** Revert only `fix(gateway): keep restart notices scoped and accurate`; restore identical-target-only deduplication, unmarked shutdown sends, and the fixed manual-resume text, then remove the HERMES-093 index row and this record. No schema, configuration, or persistent-data rollback is required.
- **Retirement:** Retire after a released upstream version suppresses redundant unthreaded home broadcasts after same-parent Telegram DM-topic notices, preserves forum/group parent and explicit home-topic delivery, marks advisories interim, renders continuation-policy-aware instructions, and passes equivalent focused regressions. Remove the fork implementation and duplicate tests rather than retaining parallel behavior.

### HERMES-097 — Make restart recovery run-correlated and durable

- **Summary:** Type delivery obligations, correlate final answers with the durable interrupted-turn token, classify historical restart notices as control rows, suppress agent replay when a terminal assistant stop already completed the model turn, and clear recovery only for the matching final answer. Persist normalized drain-time inbound in a SQLite restart inbox before acknowledging it, release a failed replay claim while continuing later rows, restore startup-gate state even when replay raises, replay after startup resume ordering, and record its handoff only after active-turn recovery durably owns continuation with the exact persisted turn token. Drain status replies are ephemeral and never recorded as final answers.
- **Surfaces:** `gateway/delivery_ledger.py`; `gateway/restart_inbox.py`; `gateway/session.py`; `gateway/run.py`; `gateway/platforms/base.py`; focused gateway tests; this record.
- **Upstream tracking:** Open upstream PR #67078 proposes run-correlated crash-safe recovery but does not provide this fork's complete typed-control-obligation and durable drain-inbox contract.
- **Upstream PR:** Open PR #67078 covers the run-token delivery subset. No upstream PR covers the complete HERMES-097 contract before publication.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_delivery_ledger.py tests/gateway/test_delivery_ledger_producer.py tests/gateway/test_restart_inbox.py tests/gateway/test_restart_resume_pending.py tests/gateway/test_clean_shutdown_marker.py tests/gateway/test_active_turn_recovery.py -q`; live QA must prove a control obligation does not clear an interrupted turn, a queued drain message survives process replacement, and each path is dispatched once.
- **Expected published commit identity:** Stable subject `fix(gateway): make restart recovery run-correlated and durable`; schema migration, source, regressions, and this record ship together.
- **Rollback:** Revert the stable subject. The additive `delivery_obligations` columns and `restart_inbox` table may remain inert; do not drop them while an older gateway could still be reading `state.db`. Queued rows not yet handed off must be reconciled before rollback.
- **Retirement:** Retire after released upstream ships run-token-correlated delivery recovery, typed control obligations, durable drain-time inbound replay, equivalent migration behavior, and end-to-end restart regressions. Remove the fork implementation and duplicate tests rather than retaining parallel state machines.
- **Additional historical subjects (optional provenance):** `fix(review): close candidate delivery blockers`.

### HERMES-099 — Preserve expanded remote update mutex paths

- **Summary:** Assign the already-safe expanded path as shell syntax, then pass the expanded variable as the Python helper's quoted argument. The advisory lock, close-on-exec behavior, detached backend contract, and update marker remain unchanged.
- **Surfaces:** `apps/desktop/electron/remote-lifecycle.ts`; `apps/desktop/electron/remote-lifecycle.test.ts`; this record.
- **Upstream tracking:** Frozen upstream cutoff `57d305d57f04ffb58fb8adef3657b166fa6e34a6` still double-quotes the expanded path and its test leaves the malformed mutex artifact on macOS. No released equivalent was identified.
- **Upstream PR:** None identified at the frozen cutoff.
- **Regression:** `cd apps/desktop && npm exec -- vitest run electron/remote-lifecycle.test.ts`; the real POSIX spawn-command test must read an empty mutex file from the selected temporary Hermes home and leave no quote-bearing path in the checkout.
- **Expected published commit identity:** Stable subject `fix(desktop): preserve expanded remote update mutex paths`; source, regression, and this record ship together.
- **Rollback:** Revert only the stable subject, remove HERMES-099's index row and record, and accept that Desktop SSH spawn serialization may lock an unintended relative path. No schema or persistent-data rollback is required.
- **Retirement:** Retire after released upstream passes the expanded-home regression and no longer creates quote-bearing mutex paths. Remove the fork implementation and duplicate test rather than retaining parallel path handling.
- **Source references from initial investigation:** `apps/desktop/'/var/.../.hermes-update-in-progress.mutex'`.

### HERMES-101 — Keep profile deletion resilient to transient process access failures

- **Summary:** Enumerate process objects without eager attributes, then read name, user, and command line inside the existing guarded body. A transiently unreadable process is skipped while profile-bound backend matching remains unchanged.
- **Surfaces:** `hermes_cli/profiles.py`; `tests/hermes_cli/test_profiles.py`; this record.
- **Upstream tracking:** Current upstream `63279301bcbdc185c1b07b98a9312eb0c862f26d` still requests eager process attributes. No released fix was identified.
- **Upstream PR:** None identified at the frozen cutoff.
- **Regression:** `scripts/run_tests.sh tests/hermes_cli/test_profiles.py -q`; a deterministic protected-process double must be skipped and the rmtree-failure test must not depend on the live host process table.
- **Expected published commit identity:** Stable subject `fix(profiles): tolerate transient macOS process access`; source, regression, and this record ship together.
- **Rollback:** Revert the stable subject and remove HERMES-101's row and record; profile deletion may again abort when macOS denies a concurrent process command-line read.
- **Retirement:** Retire after released upstream defers process attributes into per-process error guards and passes the focused regression.

### HERMES-105 — Warn on nonstandard worktree paths

- **Summary:** Inspect direct agent terminal commands for `git worktree add`, resolve literal destinations across shell separators, `git -C`, common wrappers, options, and traversal, then attach a non-blocking warning when the destination does not contain a `.worktrees` folder. Preserve legitimate external worktree use while making the repository-owned convention visible in both foreground and background tool results.
- **Surfaces:** `tools/worktree_path_guard.py`; `tools/terminal_tool.py`; `tests/tools/test_worktree_path_guard.py`; this record.
- **Upstream tracking:** Not yet filed upstream as of 2026-09-01. Hermes's native `-w`, `/worktree new`, and Kanban worktree paths already use repository-local `.worktrees/`; this patch adds advisory coverage for manual terminal commands.
- **Upstream PR:** None as of 2026-09-01. The maintained-fork PR is the only published implementation currently tracked.
- **Regression:** `scripts/run_tests.sh tests/tools/test_worktree_path_guard.py tests/tools/test_terminal_tool.py tests/tools/test_terminal_bounded_execute.py tests/tools/test_terminal_task_cwd.py tests/tools/test_terminal_output_transform_hook.py -q`; coverage must prove compliant paths remain silent, absolute and relative external paths warn, traversal cannot escape an apparent `.worktrees` path, shell separators and common wrappers remain visible, heredoc data does not create false positives, and successful foreground worktree creation returns the advisory warning.
- **Published commit identity:** Stable subjects `feat(terminal): warn on nonstandard worktree paths` and `fix(terminal): recognize compound shell separators`; reconciled by `fix(maintenance): reconcile concurrent origin baseline` after the maintained branch was rebased.
- **Rollback:** Revert the worktree-warning hunks from the reconciliation commit, remove the path guard, terminal result field, focused regression, index row, and this record. No schema, configuration, or persistent-data rollback is required.
- **Retirement:** Retire after released upstream Hermes provides equivalent advisory or enforcement for direct agent-created worktrees outside `.worktrees/`, including shell traversal and wrapper coverage, and passes equivalent focused regressions. Remove the fork implementation and duplicate tests rather than retaining parallel behavior.
- **Source references from initial investigation:** `/private/tmp`; `~/.hermes/hermes-agent-worktrees`; `~/Worktrees`.

### HERMES-114 — Bound and disclose gateway executor admission

- **Independent hypothesis (2026-09-06):** The shared 32-worker executor queues unlimited work. Controlled saturation of the actual factory blocks the next turn. Bound outstanding submissions to workers plus one waiting wave, disclose queueing at the actual turn entry point, and return an explicit not-started failure on overflow. Cancellation must not free running capacity or allow cancelled queue nodes to grow without bound.
- **Summary:** Keep the existing worker count and context propagation. No priority scheduler, additional lane, provider timeout, or runtime configuration change.
- **Surfaces:** `gateway/run_executor.py`; `gateway/run.py`; `gateway/run_turn.py`; `tests/gateway/test_gateway_executor_admission.py`.
- **Upstream tracking:** Searched open and closed executor/admission/queue issues and PRs on 2026-09-06. Open PR #75802 reserves an optional interactive lane, which exceeds this bounded-admission contract. Open PR #101044 applies a 30-second timeout to all executor work, including legitimate long turns, and does not bound queued submissions. Neither is adopted. Existing capacity, context, and shutdown behavior is retained.
- **Upstream PR:** https://github.com/NousResearch/hermes-agent/pull/75802 and https://github.com/NousResearch/hermes-agent/pull/101044 are related, not equivalent replacements.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_gateway_executor_admission.py tests/gateway/test_gateway_executor_capacity.py tests/gateway/test_shutdown_executor_quiesce.py`.
- **Rollback:** Restore the standard executor factory and context wrapper in `gateway/run.py`, direct worker submission in `gateway/run_turn.py`, and remove `gateway/run_executor.py` plus its admission-only tests. Preserve existing worker count, shutdown quiescence, and other turn lifecycle code.
- **Retirement:** Replace when upstream passes the same saturation, visible-notice, cancellation, and shutdown contracts without introducing blanket running-work timeouts.
- **Additional historical subjects (optional provenance):** `fix(hermes): close verified fork improvement gaps (#72)`.

### HERMES-115: Immutable Revision Updates

- **Summary:** Bind explicit revision updates to exact Git objects and retain source rollback identity. Stop checkout drift before service mutation and reject running-version mismatches. Ordinary branch updates retain their existing behavior.
- **Upstream tracking:** The inspected upstream-derived updater lacks an immutable revision option. Searches for update revision and exact-commit updates found no direct equivalent on 2026-09-06.
- **Upstream PR:** None after checked 2026-09-06. No upstream PR has been opened.
- **Regression:** `tests/hermes_cli/test_update_revision.py` covers real Git preparation, rollback references, drift rejection and immutable retry behavior.
- **Published commit identity:** Stable subject `feat(update): bind promotion to an immutable revision`.
- **Rollback:** Revert that subject. Retained Git references restore source only, not dependencies or user state. Runtime recovery retains its separate snapshot and health requirements.
- **Retirement:** Remove this extension when released upstream proves equivalent immutable preparation, retry and verification behavior.

### HERMES-123 — Explicit background runtime deadlines

- **Upstream tracking:** No tracking issue attached. The shared `agent.deadline` platform-safe timeout primitive is reused rather than replaced.
- **Upstream PR:** None filed by this change.
- **Contract:** An explicit terminal timeout limits background runtime. Omission preserves unbounded watchers. Expiry survives recovery without resetting its duration. Owned local and sandbox process groups receive termination grace before forced cleanup. Completion is deferred until cleanup. An unverifiable stop wakes the owner with a lost/uncertain result, never proof that external publication failed.
- **Surface:** `tools/process_registry.py`, `tools/terminal_tool.py`, and `tools/terminal_tool_background.py`. No new dependencies, scheduler, or runtime configuration.
- **Regression:** `scripts/run_tests.sh tests/tools/test_background_deadlines.py tests/tools/test_process_registry.py tests/tools/test_terminal_tool.py tests/tools/test_terminal_foreground_timeout_cap.py`. Tests use real processes and bash/sh/zsh boundaries, deadline recovery, and notification deduplication.
- **Retirement:** Retire when upstream enforces equivalent explicit deadlines and uncertainty semantics. Revert the process/terminal hunks and this regression file independently of the goal-control hunks in the shared subject. Existing checkpoints remain readable when the optional deadline field is ignored.
- **Additional historical subjects (optional provenance):** `merge: reconcile preserved maintenance with PR98 origin`; `fix(goals): make continuation dependency-aware`.

### HERMES-134 — Operator-owned link rule stops at the home

- **Origin / owner:** Found by `tests/cron/test_file_permissions.py::TestConfigFilePermissions::test_ensure_hermes_home_sets_0700` failing during the 2026-09-08 upstream sync. Upstream's new `hermes_cli/config_home.py` skips `_secure_dir` when `_directory_links` finds ANY symlink among a path's ancestors, intending to leave operator-managed mounts alone. `_directory_links` walked every parent up to `/`, so on macOS `/var -> /private/var` matched for any home under a temp dir and the home kept its umask mode 0755 instead of 0700. Upstream CI does not see this because Linux `/tmp` is a real directory. Verified: `_directory_links` returns `['/var']` for a temp home and `[]` for the real `/Users/brianle/.hermes` (currently 0700), so this machine's live home was not exposed.
- **Preserve / update:** `_directory_links` takes an optional `boundary` and considers only links at or below it; `initialize_home` passes the home. Upstream's intent is preserved exactly — a symlinked home, subdir, or `logs/curator` still suppresses securing — while a system symlink above the home no longer does. Managed mode, creation semantics and the operator-owned `logs/curator` exemption are unchanged.
- **Missing storage:** A dangling ancestor outside that permission boundary remains unavailable: native recursive mkdir cannot create its missing target through the link. `tests/hermes_cli/test_home_link_boundary.py` proves initialization fails without materializing either the target or its missing parent. Preserve this alongside the macOS permission regression.
- **Surfaces:** `hermes_cli/config_home.py`; this record. The shared test is deliberately left unmodified.
- **Upstream tracking:** Upstream bug, macOS-only. `tests/cron/test_file_permissions.py` is shared and untouched by the fork.
- **Upstream PR:** None yet. Worth filing: the rule is security-relevant and the test already encodes the intent.
- **Regression:** `.venv/bin/python -m pytest tests/cron/test_file_permissions.py -q` (7 passed). Fails against upstream's unbounded `_directory_links` on macOS with `AssertionError: 493 != 448`.
- **Rollback:** Drop the `boundary` parameter and its three call sites. Doing so reintroduces the macOS test failure and leaves a credential directory unsecured whenever any ancestor is a symlink.
- **Retirement:** Retire when upstream bounds the link scan itself.
- **Additional historical subjects (optional provenance):** `fix(config): scope the operator-owned link rule to the home`.

### HERMES-138 — Saved restart history is not live runtime state

- **Cron-aware observer deadline follow-up (2026-09-12):** `fix(update): cover cron drain in restart verification budget` makes `resolve_restart_exit_wait_budget` compose after-turn deferral, the existing `resolve_systemd_timeout_stop_sec` chat/cron/cleanup envelope, and observer startup headroom. The CLI supplies the configured cron timeout through its existing config/env parser. The observed chat=5s, after-turn=30s, cron=30s configuration now permits 115s instead of 50s; the replacement observed at approximately 76s is inside that bound. No drain policy, watchdog, signal, receipt, PID identity or fleet acceptance rule changes. All shared CLI exit-wait callers inherit the conservative envelope; a never-replaced gateway can therefore take longer to fail, but remains bounded.
- **Deadline provenance / retirement:** Existing upstream #94759/#95088 supervisor-stop primitives supply the cron cleanup reserve and stop headroom rather than introducing another fixed timeout. Upstream main `d595e636c83aa0b9606d4e914e1140ae9c796897` still has the chat-only observer formula. Credit for the preceding settling-loop adaptation remains upstream PR #102733; this is a separate resolver/wiring correction. Retire when released upstream composes the full cron-aware observer deadline and passes equivalent real-config regressions. Roll back only this follow-up commit; retain outgoing-identity and historical-warning fixes.
- **Deadline regression / activation:** Real temporary-profile YAML and the real CLI resolver feed the virtual-time command/receipt test (no mocked budget), covering a 76s successor, a longer configured cron drain, explicit cron zero, immediate wrong-SHA rejection, and bounded never/down/empty failures. Shared resolver coverage also includes chat-dominated stop and the cron env opt-out. Run `scripts/run_tests.sh -j 4 tests/gateway/test_restart_after_turn.py tests/gateway/test_restart_drain.py tests/hermes_cli/test_update_fleet_settling.py tests/hermes_cli/test_gateway_service.py tests/hermes_cli/test_update_wedged_gateway.py tests/hermes_cli/test_update_fleet_restart_pending.py tests/hermes_cli/test_update_fleet_restart_timeout.py tests/hermes_cli/test_update_restart_recovery.py tests/hermes_cli/test_update_gateway_restart_aborted.py -- -k 'not TestLaunchdUnloadedJobStderrStaysOffTerminal'` with an isolated HOME and lifecycle subprocess/signal safety fence. The pre-existing excluded class prepends a fake launchctl script to PATH and stubs gateway PID discovery to return None; the conservative audit fence rejected the executable name before subprocess execution, not a proven escape to the system launchctl binary. The real-config updater regression currently runs on macOS; equivalent Linux-native coverage is a nonblocking follow-up, not claimed here. No production update/restart is part of candidate verification; post-landing activation must separately prove successor SHA, final receipt, process exit and original notification.

- **Outgoing identity follow-up (2026-09-11):** `fix(update): retain planned gateway identities during restart` supplements cleanup-oriented PID discovery with pre-update inventory gateway PIDs. An agent-launched updater excludes its ancestor gateway from the process scan, while launchd can report a stderr wrapper. Retaining the plan's socket/state identity lets the existing bounded settling loop recognize the outgoing gateway without changing kill targets, profile discovery, the native budget, or prompt failure for a genuinely new stale successor. Regression exercises the real discovery/ancestor-walk/inventory/snapshot chain with OS responses simulated, finalized command receipts and native notice interpretation at 50s and 240s budgets. Upstream PR #102733 (Harvey-Specter-Litt, open) remains the credited source for the preceding settling-loop adaptation; related #56908 is open and #108227 closed in its favor, but neither proposal repairs this snapshot producer. Retire this follow-up when upstream preserves authoritative outgoing inventory identities and passes equivalent wrapper/ancestor coverage. Roll back only this commit's PID augmentation and tests; retain prior settling and history-warning behavior.

- **Origin / owner:** Brian's local reproduction on 2026-09-08: update marker expected `fa11f98ea7c48d00da0e6df49e058957db53ce31`; the subsequently restarted default gateway reported exactly that revision, with no discovered serve/dashboard holder. The old warning nevertheless asserted gateways were not restarted. Independent source analysis and upstream issue [#98588](https://github.com/NousResearch/hermes-agent/issues/98588) confirm the diagnostic reads saved history, not live state. Upstream PR #105417 proposes auto-clear semantics and is not adopted by this bounded change.
- **Preserve / update:** Only the shared warning text and startup guidance change. Marker generation, retention, receipt fallback and explicit updater catch-up remain authoritative and unchanged. No startup fleet probe, automatic marker clearance or unconditional restart guidance. `update --plan` reports discovered runtimes; it is not exhaustive proof that every possible holder is current.
- **Verify / rollback / retirement:** Run `tests/hermes_cli/test_update_restart_warning_history.py` and `tests/hermes_cli/test_update_fleet_restart_pending.py` in the isolated test runner. Real CLI startup against a temporary home must preserve marker bytes and emit historical wording; no marker remains quiet. Revert the warning-text block and associated assertion/test changes to roll back. Retire this overlay when upstream supplies equally truthful diagnostics or independently verified conservative reconciliation without weakening unresolved-holder safety.
- **Additional historical subjects (optional provenance):** `fix(cli): distinguish saved restart history from live state`.

### HERMES-072 — Prevent silent gateway worker starvation after ten long turns

- **Summary:** Raises the gateway-owned blocking executor ceiling from 10 to 32 and adds a deterministic regression that holds ten workers while proving the eleventh starts promptly. Existing FIFO `/steer` fallback regressions separately prove that a steer arriving before agent construction is preserved as the next turn.
- **Surfaces:** `gateway/run.py`; `tests/gateway/test_gateway_executor_capacity.py`; existing `/steer` and queue regressions.
- **Upstream tracking:** No released equivalent is present in the maintained base, and no direct issue or PR was identified after checked 2026-08-29.
- **Upstream PR:** None after checked 2026-08-29.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_gateway_executor_capacity.py tests/gateway/test_steer_fifo_overwrite.py tests/gateway/test_steer_command.py tests/gateway/test_queue_consumption.py -q` passed 10 tests with 0 failures; full gateway and repository CI remain required before publication.
- **Rollback:** Revert `fix(gateway): prevent ten-turn worker starvation`; the prior ten-worker bound returns without changing session or queue data.
- **Retirement:** Retire after released upstream removes the silent ten-turn queue cliff through equivalent bounded capacity or explicit admission behavior and passes the held-worker regression.

### HERMES-088 — Silence redundant process notifications

- **Summary:** Add an exact `NO_REPLY` reconciliation contract only to model-facing gateway process completion, watch, disabled-watch, and overflow notifications. Cover individual and coalesced completion paths, preserve substantive follow-ups and async delegation, retain the pre-refactor gateway watch payload, and keep the shared CLI, TUI, and Desktop formatter free of model-only control instructions.
- **Surfaces:** `tools/process_registry.py`; `gateway/run.py`; `cli.py`; `tui_gateway/server.py`; focused process-notification regressions; this record.
- **Upstream tracking:** NousResearch/hermes-agent#52694. The concrete one-process, one-watch-event reproduction is recorded at issue comment `5487953374`.
- **Upstream PR:** Open PR #99941 carries the mirrored implementation. Fork updates must follow reviewed changes to that PR until it merges or closes, without auto-merging upstream.
- **Regression:** `scripts/run_tests.sh tests/tools/test_process_registry.py tests/gateway/test_background_process_notifications.py tests/gateway/test_completion_delivery.py tests/gateway/test_gateway_silence_tokens.py tests/hermes_cli/test_cli_async_delegation_delivery.py tests/test_tui_gateway_server.py -q`; coverage must prove exact silence-token handling for individual and batched gateway process telemetry, unchanged substantive output, unchanged async delegation, no control-contract leakage outside the gateway, and unchanged subagent-owned watch text.
- **Expected published commit identity:** Stable subjects `fix(gateway): silence redundant process notifications` and `fix(gateway): preserve watch notification text`; source and focused regressions are mirrored from PR #99941, while this lifecycle record is fork-owned.
- **Rollback:** Revert only `fix(gateway): preserve watch notification text` and `fix(gateway): silence redundant process notifications`, then remove the HERMES-088 index row and this record. No schema, configuration, or persistent-data rollback is required.
- **Retirement:** Retire after a released upstream version prevents non-actionable process telemetry from producing redundant public replies, preserves substantive process updates and async delegation, avoids control-contract leakage on non-gateway surfaces, and passes equivalent regressions. Remove the fork implementation and duplicate tests rather than retaining parallel behavior.
