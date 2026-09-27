# Immutable releases (S2) — activation runbook

Patch identity: `seamless-restart-s2`.

## Contract and current gate

This is the fork's core update/launchd/cron release boundary, not a plugin: atomic pointer changes, update receipts, supervisor definitions and child executable pinning must agree. The source checkout remains `$HERMES_HOME/hermes-agent`; release directories are `$HERMES_HOME/releases/<exact-git-sha>`, each with a venv built at its **final path**. Shared profile state and plugins stay outside releases. Revert this unit only when upstream demonstrates the same process pinning, transactional migration and reversible fleet promotion.

**NOT ACTIVATION-READY while PR #192 is draft or any S2 acceptance item/required exact-SHA check is open.** Publishing or merging the source never authorizes running `hermes update` on the personal install or restarting its live gateway. Only the parent owner may authorize activation separately.

## Qualification evidence (disposable only)

`test_sigkill_stage_and_flip_converge_with_complete_current` kills a real child with SIGKILL after completed staging and between the `previous` and `current` atomic renames. The parent checks that `current/.release-ready` still identifies complete A, then reruns staging/promotion and observes complete B. No user-facing pause environment variable is installed; the callback is injected only by the test.

The live profile has no installed Hermes entry-point plugins. Candidate smoke imports enabled entry-point manifests, but an installed entry-point integration proof is outside S2 on this machine. S1 (#187) established real detached-worker survival across gateway process-group termination; the separate parent launchd coalition probe confirmed bootout does not kill a setsid double-fork. S2 does not redo that worker-topology proof.

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
2. Run `hermes update --plan` and inspect the whole profile/service fleet. With the parent explicitly authorizing the live restart, invoke `hermes update` once from the installed CLI. An unsuccessful stage must leave `current` and the service definition unchanged; stop rather than forcing a restart.
3. Read back `readlink "$HERMES_HOME/current"`, `readlink "$HERMES_HOME/previous"`, `$HERMES_HOME/logs/update_receipts/latest.json`, `hermes gateway status`, and the live launchd `ProgramArguments`, `WorkingDirectory` and `EnvironmentVariables` via `launchctl print "gui/$(id -u)/ai.hermes.gateway"`. Compare the new gateway's actual `code_sha` and executable/cwd with the selected release. Exercise an authorized inbound/response and check cron delivery/ledger once; ensure previously detached A workers still load from A.
4. If post-flip health fails, the **rollback point** is `previous`: under the same authorization run `hermes update --rollback` and check receipt exit/status, `current` target, fleet PID/code SHA, launchd definition and inbound response. Do not merely flip a symlink by hand while a gateway is running. If this is first migration from an in-place checkout, restore the previously saved source-checkout plist through the supported gateway definition refresh/rollback path and verify the old checkout interpreter/cwd and live process. If that path is not implemented and tested, **do not activate**.
5. Keep release A and the saved plist until live process pins, receipts, health and rollback are independently verified. Never remove a pinned release manually; no automatic rerun of interrupted cron/chat work.

## Qualification before this runbook may be used

Use a **throwaway launchd label and temp HERMES_HOME**, never `ai.hermes.gateway` or a prefix enumerated by the updater's real fleet. Prove bootout/re-bootstrap and cleanup of the exact throwaway label, A-worker loaded paths across A→B, B gateway loaded SHA, candidate-failure pointer preservation, kill-at-both-atomic-boundaries, A→B→A with receipt and fleet verification, first-migration reversal, and retention of real PID/cwd/exe and receipt pins. Run `scripts/run_tests.sh` focused files, `mise x uv@0.12.13 node@26.8.2 -- ./bin/ci preflight`, and the hosted Linux/qualification checks at the **same head SHA**. An ordinary process-group SIGTERM test does not substitute for actual `launchctl bootout` coalition behavior.
