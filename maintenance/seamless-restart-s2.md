# Immutable releases (S2) — activation runbook

Patch identity: `seamless-restart-s2`.

## Contract and current gate

Detached cron dispatch freezes both the physical tree and immutable-release module
when the scheduler loads. A module first imported through a lexical package path
after `current` moves can otherwise launch the new release from the old scheduler.
The worker's interpreter, cwd, environment, and import path all retain the loaded
installation. The real worker test holds an A dispatcher across promotion to B,
then proves its delayed worker still uses A while new B dispatchers use B.

After first-migration rollback removes `current` and `previous`, service rendering
resolves the recorded source interpreter while `updates.immutable_releases` remains
enabled. This fallback requires the completed `rolled-back` journal state, absent
pointers, the recorded source commit, and an interpreter that imports that checkout.
Broken pointers, unfinished journals, changed source commits, and unavailable source
interpreters remain hard failures. `test_gateway_source_interpreter_after_migration_rollback`
drives the real rollback and plist-rendering boundary against a disposable home.

This is the fork's core update/launchd/cron release boundary, not a plugin: atomic pointer changes, update receipts, supervisor definitions and child executable pinning must agree. **S2 is macOS launchd-only**: `updates.immutable_releases: true` fails before staging or migration on Linux/Windows and any non-launchd manager. The source checkout remains `$HERMES_HOME/hermes-agent`; release directories are `$HERMES_HOME/releases/<exact-git-sha>`, each built and smoke-tested in a unique sibling `.staging-<sha>-<uuid>` directory, with venv paths relocated before atomic publication. Shared profile state and plugins stay outside releases. Revert this unit only when upstream demonstrates the same process pinning, transactional migration and reversible fleet promotion.

**NOT ACTIVATION-READY while PR #192 is draft or any S2 acceptance item/required exact-SHA check is open.** Publishing or merging the source never authorizes running `hermes update` on the personal install or restarting its live gateway. Only the parent owner may authorize activation separately.

## Qualification evidence (disposable only)

The crash-injection matrix in `test_immutable_release_transactions.py` hard-exits a real child after every durable rename, unlink, plist install, reload marker and transaction deletion for promote, rollback, first migration and first-migration rollback. The ordinary entry point uses the **recorded intended target**; it does not re-derive rollback A from a partially changed `previous` pointer. First migration and its reversal include UUID-named disposable launchd labels and check the bytes loaded by launchd, not only the plist file. These tests predate the review-11 supervised-process acknowledgement; a simulated loaded marker or successful `launchctl` call alone does not prove that the running gateway imported the intended release.

`release-txn.json` is the write-ahead authority for pointer, plist, launchd reload and migration-journal changes. Its temp file is fsynced before atomic rename and directory fsync. It records the operation, original and intended pointers/journal, original plist backup and hash, and exact intended plist bytes and SHA-256. For a reload-required transaction, pointer and installed-plist convergence is **not activation acknowledgement**: a returned `launchctl submit`, synchronous bootstrap, positive launchd PID, or `reload_done` flag alone cannot remove the record. `acknowledge_running_release` checks pointer/journal/plist intent; the installed plist hash, label and `HERMES_HOME`; a live PID in that label's launchd-supervised process tree; its gateway argv, physical interpreter, cwd, empty `PYTHONPATH`, and the intended release ready marker/SHA (or recorded source SHA on first-migration rollback). When the gateway itself calls the acknowledgement, it also binds the observed PID to its loaded physical code root. Only then is the completion identity saved in `release-last-txn.json` and the pending record removed. The gateway's macOS supervised-child startup watcher attempts this after its own runtime status reports the same PID in `running` state; it keeps checking on later running transitions for the life of the process, and failure remains pending rather than becoming a success receipt. The candidate interpreter runs strict bundled-skills, catalog and profile-config maintenance **after staging and before promotion** so a failed migration cannot select code requiring new state. The shared post-update maintenance result controls the receipt and exit status.

One reload per switch: after the transaction's launchd reload is acknowledged, fleet handling credits and verifies that exact plist's gateway without a second kickstart. Other managed labels and manual/systemd gateways still follow their normal fleet paths; a pending-transaction retry or no-pull catch-up observes the same completion identity before deciding whether to relaunch anything.

After a crash before pointer/plist application, the recorded intent remains the recovery target; inspect it rather than inferring an inverse from `previous`. Once a reload-required intent is applied, recovery verifies its pointers, journal and installed plist, then observes the intended running gateway. It does **not** issue a second reload while waiting for acknowledgement. Its marker records a UTC issue time and intended plist SHA-256 before the callback; an issued reload is never submitted again by any updater, rollback or startup retry. If the callback failed or the marker was written before a crash and the label has no process, inspect the saved intent and label, then explicitly repair the service with the recorded plist under operator control. Do not repair while any intended-release process is starting or running. Ordinary retries remain partial and observation-only. A running old-release process after a failed callback is likewise a manual-repair case, not permission for automatic re-issue. A reload callback that returns without a matching gateway produces `reload_pending`, a partial update or rollback receipt, and no release-transition success claim. Do not manually flip symlinks, delete the transaction/backup or count a submitted helper as a successful rollback.

`test_sigkill_stage_and_flip_converge_with_complete_current` kills a real child with SIGKILL after completed staging and between the `previous` and `current` atomic renames. The parent checks that `current/.release-ready` still identifies complete A, then reruns staging/promotion and observes complete B. No user-facing pause environment variable is installed; the callback is injected only by the test.

`test_first_migration_and_source_plist_reversal_real_process` uses a temp home and `ai.hermes.s2migration.<uuid>`: the updater's `_activate_immutable_release` journals the original plist and source SHA, promotes a complete release, and reloads the throwaway launchd job; `_cmd_update_impl(--rollback)` restores the original plist bytes and a new source-checkout process. The test replaces the fleet-restart/verify collaborators with throwaway-label-only process probes, so it does **not** prove the full fleet pipeline (separate acceptance item below).

`test_launchd_resolves_current_on_each_spawn` now creates two real Git revisions and two release venvs, observes A and B launched under one throwaway job, then invokes `_cmd_update_impl(--rollback)` through the real `_restart_gateway_fleet_after_update` and `_verify_fleet_after_update` path. The test constrains discovery to its throwaway label and replaces only the gateway socket identity seam with a psutil-verified PID/cwd report from the real probe process. It checks a fresh A process, both pointers, receipt `from_sha`/`to_sha`, restarted label and one current fleet row. This is an updater/fleet process-boundary probe, not a live Hermes gateway or a messaging canary.

The live profile has no installed Hermes entry-point plugins. Candidate smoke imports enabled entry-point manifests, but an installed entry-point integration proof is outside S2 on this machine. S1 (#187) established real detached-worker survival across gateway process-group termination; the separate parent launchd coalition probe confirmed bootout does not kill a setsid double-fork. S2 does not redo that worker-topology proof.

The gateway's macOS supervised-child watcher checks for its own PID in `running` state throughout its lifetime, including after a slow boot, and performs blocking launchd/psutil/git acknowledgement checks on a worker thread rather than the event loop. The detached updater waits up to **180 seconds** after a deferred reload, polling only for the gateway's acknowledgement (the `_await_release_acknowledgement(timeout_seconds=...)` argument permits shorter tests). The updater does not issue another reload; a genuine timeout leaves `release-txn.json` pending, reports that the reload was issued and awaits acknowledgement, and exits partial. The shared release-manager helper `wait_for_release_acknowledgement` is the only observation loop. Its callers are the activation path, pending-transaction recovery, no-pull catch-up/repair, rollback, first-migration rollback, and post-swap handoff through `_cmd_update_impl`. The configured bound is `updates.release_acknowledgement_timeout_seconds` in `config.yaml`, default `180.0`; it is not an environment setting. A genuine timeout leaves the transaction and issued marker intact, reports the configured observation window and awaits acknowledgement, and exits partial. The updater does not issue another reload; inspect the label and recorded plist, then explicitly repair the service under operator control if no intended process starts. A service-not-loaded label after a callback failure or crash following the write-ahead marker cannot self-heal through retries. After verifying the intended plist's SHA-256 against `release-txn.json`, confirming the label has no loaded job or intended-release process, and obtaining operator authorization, repair with `launchctl bootstrap "gui/$(id -u)" "$RECORDED_PLIST"` using the transaction's `plist.path`. Then rerun the updater to consume the gateway acknowledgement; do not delete the issued marker or submit a second reload through recovery. The gateway `/update` launches a detached updater with a process-exit-code file, so its wait survives the old gateway's shutdown without blocking that shutdown.


## Runtime-environment parity audit

| Runtime surface | Candidate treatment and gate |
|---|---|
| Active extras and transitive packages | Infer installed optional leaf groups and orphan locked dependency closure from source interpreter and candidate `pyproject.toml`/`uv.lock`; `uv sync --frozen --extra` uses candidate lock pins and the source interpreter's Python version. Restore source packages absent from lock with `uv pip --no-deps`; fail staging if **any** source distribution is missing (except project itself), if an unlocked version changes, or if installed plugin entry points disappear. `Provides-Extra` lists availability, not the install's selected extras. |
| Shared `~/.hermes/plugins` | Never copy mutable plugins into releases. Smoke-import enabled directory and installed entry-point plugins with candidate Python under a copied disposable home; candidate dependency parity prevents missing installed plugin requirements, though plugin `register()` remains outside this pre-activation smoke. |
| Console scripts, editable `.pth`, native artifacts | Build a fresh venv within unique staging, rewrite text paths including entry-point shebangs and `.pth`, remove regenerable compiled `__pycache__/*.pyc` with staging paths, fail for any other binary containing one. `UV_COMPILE_BYTECODE=0` explicitly leaves uv bytecode compilation to runtime; smoke can independently generate caches. Native wheels come from locked uv platform resolution, not copied source binaries. Qualification exercises relocated `hermes` and imports after publication. |
| Node/web assets and ignored generated files | Archive the exact fetched SHA into a private build-aside directory, run npm and the web build **there**, then build the candidate venv there. Never copy source `web_dist` or run npm in the source checkout: A's ignored generated files cannot become B's assets. A failed build cannot publish a ready candidate. |
| Supervisor and cron env | Launchd resolves `current` for executable, cwd and venv PATH. Detached cron pins physical release Python/cwd, `HERMES_RELEASE`, `PYTHONPATH`, `VIRTUAL_ENV`, and leading venv-bin PATH, so subsequent pointer flips cannot change its subprocess interpreter. |
| Config and secrets | Shared `$HERMES_HOME/config.yaml`, `.env`, skills, cron and memory remain profile state outside the archived code. The live source has no code-root `.env` or `cli-config.yaml`; a future source-local file is not copied into releases and must be migrated to shared profile before activation. |

## Pre-activation inventory (read-only)

```bash
export HERMES_HOME="$HOME/.hermes"
export SOURCE="$HERMES_HOME/hermes-agent"
cd "$SOURCE"
git status --short --branch
git rev-parse HEAD
git ls-remote origin refs/heads/main
du -sh "$SOURCE" "$SOURCE/venv"
df -h "$HERMES_HOME"
readlink "$HERMES_HOME/current" || true
readlink "$HERMES_HOME/previous" || true
hermes update --plan
hermes gateway status
launchctl print "gui/$(id -u)/ai.hermes.gateway"  # inspect only; do not bootout here
```

Measured before activation: the checkout including all worktrees and generated artifacts uses **32 GiB**, and the existing `venv/` uses **411 MiB**. These overlap: do not add these values as independent usage. A warm-cache `uv sync --frozen` at a final disposable release path took **1.60 s** on this Mac, with a 63-package core install. This is not a bound for all extras, source-copy time, plugin smoke or a cold cache. Check actual free space and compute a release's source plus venv size in a scratch rehearsal before activation; reserve room for current + previous + three rollback releases and any live/receipt pins. The existing source checkout stays in place.

## Parent-authorized activation (only after all acceptance gates)

1. Confirm the exact merged/qualified SHA in `git rev-parse HEAD`, clean checkout, enough free disk, a recent **full** backup for state/file-loss recovery, and dual-version shared-plugin/schema compatibility. Record the existing launchd plist and its SHA at a safe backup destination before migration; the original plist is the **first-migration rollback point**. Record the old service PID/start fingerprint and runtime `code_sha` from `$HERMES_HOME/gateway_state.json` (or the current profile's status file). Do not infer runtime identity from source HEAD.
2. Run `hermes update --plan` and inspect the whole profile/service fleet. First migration is opt-in (`updates.immutable_releases: true`); absent a valid `current` release the flag is mandatory, for **both** fetched updates and no-pull reconciliation. An existing ready release under `releases/` can continue without the flag. Under the parent's separate activation authorization, set and verify the flag, then run the installed updater with restart authorization. Immutable mode fetches the branch into the source Git object store without switching, merging, resetting, or syncing the source venv. It stages the exact fetched SHA in `releases/.staging-*`, including Node/web and a newly locked venv, and hands off to that release's own Python and updater code before promotion. `--no-gateway-restart` stages only when no unacknowledged pending reload exists; a later normal update may promote the staged release. Source A remains intact as rollback target. The migration journal binds A's source interpreter and original plist; a failed stage must leave `current` and launchd unchanged.
3. Read back `readlink "$HERMES_HOME/current"`, `readlink "$HERMES_HOME/previous"`, `release-txn.json` if present, `$HERMES_HOME/logs/update_receipts/latest.json`, `hermes gateway status`, and the live launchd `ProgramArguments`, `WorkingDirectory` and `EnvironmentVariables` via `launchctl print "gui/$(id -u)/ai.hermes.gateway"`. Compare the installed plist's exact hash with the transaction's `intended_sha256`, then confirm the launchd-supervised PID and its actual gateway code root/SHA, interpreter and cwd match the selected release. An on-disk plist or positive PID alone is insufficient; a pending record or partial receipt means activation remains unresolved. Exercise an authorized inbound/response and check cron delivery/ledger once; ensure previously detached A workers still load from A.
4. If post-flip health fails, run the separately authorized `hermes update --rollback` and verify process/root/SHA, receipt and service. On first migration the source A tree and venv never moved: rollback checks source SHA, restores the original plist bytes, removes both `current` and `previous` pointers, marks the journal `rolled-back`, and restarts A. A source process must import A's dependency API successfully. If the reload is unacknowledged, the **recorded rollback target remains pending**; do not claim A is running from the changed plist/pointers. Inspect the transaction and live supervised process before retrying: an already-applied pending reload is observation-only on retry and will not itself issue another bootout/bootstrap. Escalate if the intended gateway never starts or the intent diverges; never manually toggle symlinks or infer A from the moved pointers. There is no Git reset or source dependency sync. With the flag false, subsequent pulled and no-pull updates stay on the legacy source path and do not re-promote.
5. Keep current and previous, three newest additional rollback releases, explicit receipt pins, and any release referenced by a same-UID process's readable executable/cwd/argv/environment. Another UID's unreadable process and a same-UID non-Python process with unreadable identity do not pin all releases; an unreadable same-UID Python process does. A missing owner identity also fails closed. The real-host retention test retains a child pinned to an old release and prunes excess releases.

## No-pull reconciliation state table

`hermes_cli.update_cmd._reconcile_immutable_release` owns no-pull reconciliation. The shared activation predicate requires explicit opt-in unless `current` resolves to a ready release under `releases/`. Its original seven axes are opt-in, `current` (`absent/equal/different` relative to the fetched SHA), candidate (`none/staged/failed-partial`), journal (`none/in-progress/done/rolled-back`), launchd service (`none/source/current/stale-release`), running fleet root and SHA (`none/source/current/other`), and restart deferral. A leading eighth axis is **pending transaction**: complete it before every other row, then inspect the resulting state. `test_reconcile_matrix` covers distinguishing outcomes across these axes rather than the full Cartesian product.

| Pending | Enabled | Current | Candidate | Journal | Service | Running | Defer | Action |
|---|---|---|---|---|---|---|---|---|
| true | * | * | * | * | * | * | false | verify recorded intent and observed gateway; if unacknowledged, stop partial without reloading an already-applied intent |
| true | * | * | * | * | * | * | true | only consume an already-observed acknowledgement; otherwise stop partial before a launchctl action |
| false | false | absent | * | * | * | * | * | no-op (legacy/reversed source; never opt in implicitly) |
| false | true | absent | * | rolled-back | * | * | * | explicit re-opt-in permits a fresh migration from untouched A |
| false | * | equal | none or failed-partial | * | * | * | * | fail-with-message (active release is incomplete) |
| false | * | equal | staged | * | none or current | none or current | * | no-op |
| false | * | equal | staged | * | source or stale-release, or running source/other | * | true | defer-record |
| false | * | equal | staged | * | source or stale-release, or running source/other | * | false | repair-service (refresh launchd, arm fleet restart if runtime stale) |
| false | * | absent or different | * | * | * | * | true | defer-record (stage incomplete/missing artifact first; never flip/reload) |
| false | * | absent or different | staged | * | * | * | false | activate-staged (validate build prerequisites; promote) |
| false | * | absent or different | none or failed-partial | * | * | * | false | build+activate if absent; an existing failed-partial target refuses replacement until an operator proves it unpinned and removes it safely |

Unreachable rows without a pending transaction: absent pointer with a `current` service/process; different pointer with no journal. An in-progress journal with a ready `current` can be an interrupted pre-transaction migration and is not automatically unreachable. A rollback is represented by **absent `current` and `previous` plus journal `state=rolled-back`**; no source-targeting `current` pointer is valid. Unknown combinations fail closed. Service repair failure is partial. A no-restart request checks for a pending transaction **before** ordinary update recovery, including when restart prohibition comes from gateway/cron policy rather than the literal CLI flag. It may consume an already observed matching gateway acknowledgement, but otherwise returns a partial receipt and exit 1 without staging, changing pointers/plist or invoking launchctl. The no-pull catch-up path applies the same guard. A normal update also stops partial on an applied but unacknowledged reload instead of replaying it; the operator must resolve the supervised gateway mismatch. Pending fleet catch-up runs only after release reconciliation, and `--no-gateway-restart` does not restart the caller's gateway.

## Strict maintenance filesystem failures

The immutable activation gate requests fatal catalog-cache writes and rejects
`sync_skills()` results carrying copy/update failures from each profile's real
subprocess. Diagnostics identify the profile, skill destination and filesystem
error. The gate keeps its existing exception contract: its caller blocks
promotion on exceptions, not on a false return value. Legacy catalog seeding and
skill sync remain best-effort; user-modified/deleted, suppressed, externally
provided and opted-out skills retain their existing policy.

Regression: `scripts/run_tests.sh tests/hermes_cli/test_update_immutable_maintenance.py
tests/hermes_cli/test_model_catalog.py tests/tools/test_skills_sync.py`. Real files
obstruct catalog parents, skill category parents and update backup directories;
the gate accepted all three on the unfixed source and now rejects them. Helper
regressions also prove non-strict calls keep returning normally. No live profile
or service is used.

## Qualification before this runbook may be used

Use a **throwaway launchd label and disposable HERMES_HOME**, never `ai.hermes.gateway` or a prefix enumerated by the updater's real fleet. Prove bootout/re-bootstrap and cleanup of the exact throwaway label; A-worker loaded paths across A→B; B gateway loaded SHA; failed submit/bootstrap and a crash after pointer/plist mutation leave a durable pending transaction; the intended gateway's startup acknowledgement, not a helper return or marker, consumes it after checking loaded plist hash, supervised PID, physical code root/SHA, executable and cwd. Prove that retry while unacknowledged does not re-bootout, and that `--no-gateway-restart` fails before service mutation with a partial receipt. Also prove hard-exit after every pointer, plist, journal, reload-ack and transaction-cleanup mutation for promote, rollback, first migration and first-migration rollback; A→B→A with receipt and fleet verification; first-migration reversal; and retention of real PID/cwd/exe and receipt pins. Run `scripts/run_tests.sh` focused files, `mise x uv@0.12.13 node@26.8.2 -- ./bin/ci preflight`, and the hosted Linux/qualification checks at the **same head SHA**. An ordinary process-group SIGTERM test does not substitute for actual `launchctl bootout` coalition behavior.


## Native PM Checkpoint Migration

The pre-tip checkpoint uses native PM tools to build archived web assets in a build-owned tool store. Staging preserves directly installed optional features and all plugin entry points. Incidental transitive packages from obsolete environments cannot enable an unrelated feature. `hindsight` selects the client SDK, while `hindsight-embedded` explicitly selects the embedded server. Candidate staging uses the active runtime interpreter as the source distribution authority. Real archived staging and plugin import smoke must pass before promotion.

The upstream compression default remains ratio-based. Brian's prior 256000-token cap must be set through supported `compression.threshold_tokens` profile configuration before activation, preserving his effective behavior while retiring the personal core default.

## Native Interpreter Acknowledgement

Homebrew Framework Python launches a kernel image under `Python.app`, which differs from the virtual environment launcher's resolved symlink. Release acknowledgement now observes the selected interpreter's kernel executable through an isolated process probe, then compares that trusted result with the supervised process. The bounded probe cache includes the launcher path and file identity. Probe failure leaves the transaction pending. It does not relax the loaded-root, supervisor, cwd, plist, SHA or profile checks. Immutable subprocess text boundaries decode UTF-8 explicitly.

The real disposable launchd tests exercise Framework Python and standalone Python without changing the live label. Minimal fixture environments include the actual process-observer dependency. Candidate plist tests select the native `release_target` interface and verify its interpreter PATH. They do not require upstream's removed `VIRTUAL_ENV` export. The refusal-only recovery fixture sets the supported acknowledgement observation window to zero while retaining the assertion that no reload callback executes. Full exact-candidate qualification and independent review remain required.

The two-release native fixture owns a real minimal source `.venv`, so optional-feature inference does not inherit unrelated cloud, browser and messaging dependencies from the suite interpreter. It retains the full tracked source tree, real archive and web builds, native PM core installation, plugin smoke, unmocked distribution preservation, rollback, retention and supervisor acknowledgement. The ordinary per-file timeout remains unchanged.

## Selected Release Launchers

Native PM source services persist the installation launcher, which intentionally follows the source install's current Python tool. Reusing that launcher for an immutable release discarded the release interpreter and loaded the source checkout in both the stderr wrapper and gateway. Release plist generation now binds both runtime commands to the explicitly selected release root and interpreter, while ordinary source services retain their installation launcher. The generated-plist invariant executes the source service and A→B→A release commands with disposable payloads, checking imported roots, bootstrap roots, interpreter identity and external supervision. It fails on the unchanged renderer because the source payload runs instead of the selected release. Upstream PR #130125 addresses a related managed-workspace canonical-root defect, not this fork-owned immutable selection. Retire this adaptation only when upstream's native release renderer demonstrates the same selected-tree and interpreter contract.

## Generated Launcher Acknowledgement

The supervised acknowledgement accepted only `python -m hermes_cli.main`, but the release plist this unit generates starts the gateway as `python -I -c <bootstrap> gateway run --external-supervisor`. Every immutable update therefore activated the intended release, then timed out waiting for an acknowledgement it could never observe, leaving `release-txn.json` pending and the receipt `partial`. The acknowledgement now also accepts that launcher, but only when its `-c` code equals the bootstrap `runtime_command` renders for the intended release root. An arbitrary `-c` program or another release's launcher is rejected, and the interpreter image, cwd and `PYTHONPATH` checks are unchanged. Guard: `test_generated_launchd_gateway_acknowledges_only_its_release` in `tests/hermes_cli/test_immutable_release_transactions.py`, driven by the real generated plist. It fails on the unchanged matcher.
