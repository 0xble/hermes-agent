# Immutable releases (S2) — activation runbook

Patch identity: `seamless-restart-s2`.

## Contract and current gate

This is the fork's core update/launchd/cron release boundary, not a plugin: atomic pointer changes, update receipts, supervisor definitions and child executable pinning must agree. **S2 is macOS launchd-only**: `updates.immutable_releases: true` fails before staging or migration on Linux/Windows and any non-launchd manager. The source checkout remains `$HERMES_HOME/hermes-agent`; release directories are `$HERMES_HOME/releases/<exact-git-sha>`, each built and smoke-tested in a unique sibling `.staging-<sha>-<uuid>` directory, with venv paths relocated before atomic publication. Shared profile state and plugins stay outside releases. Revert this unit only when upstream demonstrates the same process pinning, transactional migration and reversible fleet promotion.

**NOT ACTIVATION-READY while PR #192 is draft or any S2 acceptance item/required exact-SHA check is open.** Publishing or merging the source never authorizes running `hermes update` on the personal install or restarting its live gateway. Only the parent owner may authorize activation separately.

## Qualification evidence (disposable only)

The crash-injection matrix in `test_immutable_release_transactions.py` hard-exits a real child after every durable rename, unlink, plist install, reload marker and transaction deletion for promote, rollback, first migration and first-migration rollback. The ordinary entry point then replays the **recorded intended target**; it does not re-derive rollback A from a partially changed `previous` pointer. First migration and its reversal include UUID-named disposable launchd labels and check the bytes loaded by launchd, not only the plist file. A failed reload leaves the transaction pending for retry; no exception handler rewinds the pointers.

`release-txn.json` is the single write-ahead authority for pointer, plist, launchd reload and migration-journal changes. Its temp file is fsynced before atomic rename and directory fsync. It records operation, original/current and previous targets, intended targets, original and intended journal, plist backup and both SHA-256 hashes, and reload completion. Every step replays idempotently; the record is removed only after pointers, journal and plist are checked against intent. A pending record is completed before update, rollback and no-pull reconciliation proceed. The candidate interpreter runs strict bundled-skills, catalog and profile-config maintenance **after staging and before promotion** so a failed migration cannot select code requiring new state. The shared post-update maintenance result controls the receipt and exit status.

`test_sigkill_stage_and_flip_converge_with_complete_current` kills a real child with SIGKILL after completed staging and between the `previous` and `current` atomic renames. The parent checks that `current/.release-ready` still identifies complete A, then reruns staging/promotion and observes complete B. No user-facing pause environment variable is installed; the callback is injected only by the test.

`test_first_migration_and_source_plist_reversal_real_process` uses a temp home and `ai.hermes.s2migration.<uuid>`: the updater's `_activate_immutable_release` journals the original plist and source SHA, promotes a complete release, and reloads the throwaway launchd job; `_cmd_update_impl(--rollback)` restores the original plist bytes and a new source-checkout process. The test replaces the fleet-restart/verify collaborators with throwaway-label-only process probes, so it does **not** prove the full fleet pipeline (separate acceptance item below).

`test_launchd_resolves_current_on_each_spawn` now creates two real Git revisions and two release venvs, observes A and B launched under one throwaway job, then invokes `_cmd_update_impl(--rollback)` through the real `_restart_gateway_fleet_after_update` and `_verify_fleet_after_update` path. The test constrains discovery to its throwaway label and replaces only the gateway socket identity seam with a psutil-verified PID/cwd report from the real probe process. It checks a fresh A process, both pointers, receipt `from_sha`/`to_sha`, restarted label and one current fleet row. This is an updater/fleet process-boundary probe, not a live Hermes gateway or a messaging canary.

The live profile has no installed Hermes entry-point plugins. Candidate smoke imports enabled entry-point manifests, but an installed entry-point integration proof is outside S2 on this machine. S1 (#187) established real detached-worker survival across gateway process-group termination; the separate parent launchd coalition probe confirmed bootout does not kill a setsid double-fork. S2 does not redo that worker-topology proof.

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
2. Run `hermes update --plan` and inspect the whole profile/service fleet. First migration is opt-in (`updates.immutable_releases: true`); absent a valid `current` release the flag is mandatory, for **both** fetched updates and no-pull reconciliation. An existing ready release under `releases/` can continue without the flag. Under the parent's separate activation authorization, set and verify the flag, then run the installed updater with restart authorization. Immutable mode fetches the branch into the source Git object store without switching, merging, resetting, or syncing the source venv. It stages the exact fetched SHA in `releases/.staging-*`, including Node/web and a newly locked venv, and hands off to that release's own Python and updater code before promotion. `--no-gateway-restart` stages only; a later normal update promotes the staged release. Source A remains intact as rollback target. The migration journal binds A's source interpreter and original plist; a failed stage must leave `current` and launchd unchanged.
3. Read back `readlink "$HERMES_HOME/current"`, `readlink "$HERMES_HOME/previous"`, `$HERMES_HOME/logs/update_receipts/latest.json`, `hermes gateway status`, and the live launchd `ProgramArguments`, `WorkingDirectory` and `EnvironmentVariables` via `launchctl print "gui/$(id -u)/ai.hermes.gateway"`. Compare the new gateway's actual `code_sha` and executable/cwd with the selected release. Exercise an authorized inbound/response and check cron delivery/ledger once; ensure previously detached A workers still load from A.
4. If post-flip health fails, run the separately authorized `hermes update --rollback` and verify process/root/SHA, receipt and service. On first migration the source A tree and venv never moved: rollback checks source SHA, restores the original plist bytes, removes both `current` and `previous` pointers, marks the journal `rolled-back`, and restarts A. A source process must import A's dependency API successfully. If reload fails, the **recorded rollback target remains pending**; retry the updater to roll forward to A, never manually toggle symlinks or infer A from the moved pointers. There is no Git reset or source dependency sync. With the flag false, subsequent pulled and no-pull updates stay on the legacy source path and do not re-promote.
5. Keep release A and the saved plist until live process pins, receipts, health and rollback are independently verified. Never remove a pinned release manually; no automatic rerun of interrupted cron/chat work.

## No-pull reconciliation state table

`hermes_cli.update_cmd._reconcile_immutable_release` owns no-pull reconciliation. The shared activation predicate requires explicit opt-in unless `current` resolves to a ready release under `releases/`. Its original seven axes are opt-in, `current` (`absent/equal/different` relative to the fetched SHA), candidate (`none/staged/failed-partial`), journal (`none/in-progress/done/rolled-back`), launchd service (`none/source/current/stale-release`), running fleet root and SHA (`none/source/current/other`), and restart deferral. A leading eighth axis is **pending transaction**: complete it before every other row, then inspect the resulting state. `test_reconcile_matrix` covers the Cartesian cases.

| Pending | Enabled | Current | Candidate | Journal | Service | Running | Defer | Action |
|---|---|---|---|---|---|---|---|---|
| true | * | * | * | * | * | * | * | complete recorded transaction first; repeat reconciliation |
| false | false | absent | * | none or rolled-back | * | * | * | no-op (legacy/reversed source; never opt in implicitly) |
| false | true | absent | * | rolled-back | * | * | * | explicit re-opt-in permits a fresh migration from untouched A |
| false | * | equal | none or failed-partial | * | * | * | * | fail-with-message (active release is incomplete) |
| false | * | equal | staged | * | none or current | none or current | * | no-op |
| false | * | equal | staged | * | source or stale-release, or running source/other | * | true | defer-record |
| false | * | equal | staged | * | source or stale-release, or running source/other | * | false | repair-service (refresh launchd, arm fleet restart if runtime stale) |
| false | * | absent or different | * | * | * | * | true | defer-record (stage incomplete/missing artifact first; never flip/reload) |
| false | * | absent or different | staged | * | * | * | false | activate-staged (validate build prerequisites; promote) |
| false | * | absent or different | none or failed-partial | * | * | * | false | build+activate if absent; an existing failed-partial target refuses replacement until an operator proves it unpinned and removes it safely |

Unreachable rows without a pending transaction: absent pointer with a `current` service/process; different pointer with no journal. An in-progress journal with a ready `current` can be an interrupted pre-transaction migration and is not automatically unreachable. A rollback is represented by **absent `current` and `previous` plus journal `state=rolled-back`**; no source-targeting `current` pointer is valid. Unknown combinations fail closed. Service repair failure is partial. Pending fleet catch-up runs only after release reconciliation, and `--no-gateway-restart` does not restart the caller's gateway.

## Qualification before this runbook may be used

Use a **throwaway launchd label and disposable HERMES_HOME**, never `ai.hermes.gateway` or a prefix enumerated by the updater's real fleet. Prove bootout/re-bootstrap and cleanup of the exact throwaway label; A-worker loaded paths across A→B; B gateway loaded SHA; failed reload leaves a durable pending transaction that rolls **forward** on retry; hard-exit after every pointer, plist, journal, reload-ack and transaction-cleanup mutation for promote, rollback, first migration and first-migration rollback; A→B→A with receipt and fleet verification; first-migration reversal; and retention of real PID/cwd/exe and receipt pins. Run `scripts/run_tests.sh` focused files, `mise x uv@0.12.13 node@26.8.2 -- ./bin/ci preflight`, and the hosted Linux/qualification checks at the **same head SHA**. An ordinary process-group SIGTERM test does not substitute for actual `launchctl bootout` coalition behavior.
