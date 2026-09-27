# Immutable releases (S2) — activation runbook

Patch identity: `seamless-restart-s2`.

## Contract and current gate

This is the fork's core update/launchd/cron release boundary, not a plugin: atomic pointer changes, update receipts, supervisor definitions and child executable pinning must agree. **S2 is macOS launchd-only**: `updates.immutable_releases: true` fails before staging or migration on Linux/Windows and any non-launchd manager. The source checkout remains `$HERMES_HOME/hermes-agent`; release directories are `$HERMES_HOME/releases/<exact-git-sha>`, each built and smoke-tested in a unique sibling `.staging-<sha>-<uuid>` directory, with venv paths relocated before atomic publication. Shared profile state and plugins stay outside releases. Revert this unit only when upstream demonstrates the same process pinning, transactional migration and reversible fleet promotion.

**NOT ACTIVATION-READY while PR #192 is draft or any S2 acceptance item/required exact-SHA check is open.** Publishing or merging the source never authorizes running `hermes update` on the personal install or restarting its live gateway. Only the parent owner may authorize activation separately.

## Qualification evidence (disposable only)

`test_sigkill_stage_and_flip_converge_with_complete_current` kills a real child with SIGKILL after completed staging and between the `previous` and `current` atomic renames. The parent checks that `current/.release-ready` still identifies complete A, then reruns staging/promotion and observes complete B. No user-facing pause environment variable is installed; the callback is injected only by the test.

`test_first_migration_and_source_plist_reversal_real_process` uses a temp home and `ai.hermes.s2migration.<uuid>`: the updater's `_activate_immutable_release` journals the original plist and source SHA, promotes a complete release, and reloads the throwaway launchd job; `_cmd_update_impl(--rollback)` restores the original plist bytes and a new source-checkout process. The test replaces the fleet-restart/verify collaborators with throwaway-label-only process probes, so it does **not** prove the full fleet pipeline (separate acceptance item below).

`test_launchd_resolves_current_on_each_spawn` now creates two real Git revisions and two release venvs, observes A and B launched under one throwaway job, then invokes `_cmd_update_impl(--rollback)` through the real `_restart_gateway_fleet_after_update` and `_verify_fleet_after_update` path. The test constrains discovery to its throwaway label and replaces only the gateway socket identity seam with a psutil-verified PID/cwd report from the real probe process. It checks a fresh A process, both pointers, receipt `from_sha`/`to_sha`, restarted label and one current fleet row. This is an updater/fleet process-boundary probe, not a live Hermes gateway or a messaging canary.

The live profile has no installed Hermes entry-point plugins. Candidate smoke imports enabled entry-point manifests, but an installed entry-point integration proof is outside S2 on this machine. S1 (#187) established real detached-worker survival across gateway process-group termination; the separate parent launchd coalition probe confirmed bootout does not kill a setsid double-fork. S2 does not redo that worker-topology proof.

## Runtime-environment parity audit

| Runtime surface | Candidate treatment and gate |
|---|---|
| Active extras and transitive packages | Infer installed optional leaf groups and orphan locked dependency closure from source interpreter and candidate `pyproject.toml`/`uv.lock`; `uv sync --frozen --extra` uses candidate lock pins. Restore source packages absent from lock with `uv pip --no-deps`; fail staging if **any** source distribution is missing (except project itself), if an unlocked version changes, or if installed plugin entry points disappear. `Provides-Extra` lists availability, not the install's selected extras. |
| Shared `~/.hermes/plugins` | Never copy mutable plugins into releases. Smoke-import enabled directory and installed entry-point plugins with candidate Python under a copied disposable home; candidate dependency parity prevents missing installed plugin requirements, though plugin `register()` remains outside this pre-activation smoke. |
| Console scripts, editable `.pth`, native artifacts | Build a fresh venv within unique staging, rewrite text paths including entry-point shebangs and `.pth`, remove regenerable compiled `__pycache__/*.pyc` with staging paths, fail for any other binary containing one. `UV_COMPILE_BYTECODE=0` explicitly leaves uv bytecode compilation to runtime; smoke can independently generate caches. Native wheels come from locked uv platform resolution, not copied source binaries. Qualification exercises relocated `hermes` and imports after publication. |
| Node/web assets and ignored generated files | Update prerequisite builds `web_dist`; Git archive stages tracked sources and grafts only source `hermes_cli/web_dist`. The dashboard requires `index.html` and assets; the release smoke must check them when the source has a bundle. `tui_dist` and `hermes_cli/scripts` are absent from this live source and not required by the gateway. Unknown ignored generated state is deliberately not archived. |
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
2. Run `hermes update --plan` and inspect the whole profile/service fleet. The first release migration is **opt-in**: `updates.immutable_releases` defaults to false, so a normal update on a checkout with no `current` release pointer does not stage a release, create `releases/`, or touch launchd. After the parent explicitly authorizes activation, set `hermes config set updates.immutable_releases true` in the intended profile, verify it reads back true, then invoke `hermes update` once from the installed CLI with authorization for its restart. Existing release layouts continue staging without a new opt-in. `--no-gateway-restart` stages only and does not flip `current` or reload launchd. A later normal update must promote a pending/staged transition before restarting the fleet, even when Git has no newer commit; a genuine no-op (`HEAD == current` with no transition) leaves the release untouched. Failed web/Node prerequisites make the update partial instead of reporting success on the old release. The migration journal records a validated source interpreter, allowing a release-origin update to re-enter the recorded external environment or source `venv/` / `.venv/`. An unsuccessful stage must leave `current` and the service definition unchanged; stop rather than forcing a restart.
3. Read back `readlink "$HERMES_HOME/current"`, `readlink "$HERMES_HOME/previous"`, `$HERMES_HOME/logs/update_receipts/latest.json`, `hermes gateway status`, and the live launchd `ProgramArguments`, `WorkingDirectory` and `EnvironmentVariables` via `launchctl print "gui/$(id -u)/ai.hermes.gateway"`. Compare the new gateway's actual `code_sha` and executable/cwd with the selected release. Exercise an authorized inbound/response and check cron delivery/ledger once; ensure previously detached A workers still load from A.
4. If post-flip health fails, the **rollback point** is `previous`: under the same authorization run `hermes update --rollback` and check receipt exit/status, `current` target, fleet PID/code SHA, launchd definition and inbound response. Do not merely flip a symlink by hand while a gateway is running. On first migration, the updater records the source HEAD **before** pulling B; `hermes update --rollback` refuses a dirty source, restores that recorded source SHA A through a checked Git reset, then restores the exact source plist bytes and restarts A. If the source revision, plist bytes, receipt, fleet verification, or runtime identity differ from A, stop and repair before further updates. A failed source-plist reload attempts to restore the pre-rollback checkout revision as well as pointers and plist.
5. Keep release A and the saved plist until live process pins, receipts, health and rollback are independently verified. Never remove a pinned release manually; no automatic rerun of interrupted cron/chat work.

## No-pull reconciliation state table

`hermes_cli.update_cmd._reconcile_immutable_release` owns the no-pull decision. It observes seven axes before any catch-up restart: opt-in (`enabled`), `current` pointer (`absent/equal/different` from HEAD's release), candidate (`none/staged/failed-partial`; staged requires the exact `.release-ready` SHA), journal (`none/in-progress/done/rolled-back`), installed launchd definition (`none/source/current/stale-release`), running fleet root **and** SHA (`none/source/current/other`), and `--no-gateway-restart` (`defer`). Ordered rows below use `*` as any state; the first match wins. `test_reconcile_matrix` checks the complete Cartesian product and explicitly rejects physically unreachable combinations rather than silently treating them as success.

| Enabled | Current | Candidate | Journal | Service | Running | Defer | Action |
|---|---|---|---|---|---|---|---|
| false | absent | * | none or rolled-back | * | * | * | no-op (legacy/reversed source; never opt in implicitly) |
| true | absent | * | rolled-back | * | * | * | fail-with-message (source pointer reversal needs explicit recovery) |
| * | equal | none or failed-partial | * | * | * | * | fail-with-message (active release is incomplete) |
| * | equal | staged | * | none or current | none or current | * | no-op |
| * | equal | staged | * | source or stale-release, or running source/other | * | true | defer-record |
| * | equal | staged | * | source or stale-release, or running source/other | * | false | repair-service (refresh launchd, arm fleet restart if runtime stale) |
| * | absent or different | * | * | * | * | true | defer-record (stage incomplete/missing artifact first; never flip/reload) |
| * | absent or different | staged | * | * | * | false | activate-staged (validate build prerequisites; promote) |
| * | absent or different | none or failed-partial | * | * | * | false | build+activate if absent; an existing failed-partial target refuses replacement until an operator proves it unpinned and removes it safely |

Unreachable rows: absent pointer with a `current` service/process or a done journal; non-absent release pointer with an in-progress/rolled-back journal; different pointer with no journal. A rolled-back source pointer is represented by `absent` for decision purposes. Any unrecognized state fails closed. Service repair failure is partial, never a successful no-op. The existing fleet catch-up consumes a newly armed restart marker after release reconciliation; `--no-gateway-restart` does not restart the caller's gateway.

## Qualification before this runbook may be used

Use a **throwaway launchd label and temp HERMES_HOME**, never `ai.hermes.gateway` or a prefix enumerated by the updater's real fleet. Prove bootout/re-bootstrap and cleanup of the exact throwaway label, A-worker loaded paths across A→B, B gateway loaded SHA, candidate-failure pointer preservation, kill-at-both-atomic-boundaries, A→B→A with receipt and fleet verification, first-migration reversal, and retention of real PID/cwd/exe and receipt pins. Run `scripts/run_tests.sh` focused files, `mise x uv@0.12.13 node@26.8.2 -- ./bin/ci preflight`, and the hosted Linux/qualification checks at the **same head SHA**. An ordinary process-group SIGTERM test does not substitute for actual `launchctl bootout` coalition behavior.
