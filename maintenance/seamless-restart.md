# Seamless Restart Phase 1 Implementation Plan

Patch identity: `seamless-restart` (legacy single-gateway path).

## Shutdown Notice Routing

Use upstream's `_delivery_target_key` for active chats, served home channels,
restart requesters, and already delivered update notices. Derive its profile
from the resolved adapter's native `_owning_profile`, including shared-bot
satellites. Positive Telegram IDs remain separate conversations for separate
bots. Shared groups receive one notice, and private-topic parent suppression
remains bound to the actual adapter. Native routing invariants live in
`tests/gateway/test_multiplex_notice_egress_profile_adapter.py`.

## Resume Marker Freshness

Startup resume admission and pending follow-up recovery use upstream's
`_is_fresh_gateway_interruption` helper. Its epoch comparison handles local
markers across DST and timezone-aware markers without changing the configured
freshness window. Keep the existing DST invariants in
`tests/gateway/test_restart_resume_pending.py` and queued-replay coverage in
`tests/gateway/test_resume_queued_followup.py`.

> **Status (2026-09-28):** S1–S3, H1, S4.1 durable outbox and G1 guardian shipped on fork `main`; the live gateway still restarts drain-first. The S4.2 router/executor split stopped after two failed spikes and was not built. The overlap and forward-only handover design was later withdrawn; see the withdrawal note below. Later changes follow repository policy, verification, review and separate runtime-activation authority.

**Goal:** Promote a new Hermes gateway release without interrupting active cron executions or in-flight conversations, while making failure and rollback observable.

**Architecture:** First detach macOS cron workers from the launchd gateway job and pin their code, then install immutable releases behind an atomic `current` pointer. Add bounded worker termination and truthful ledger reconciliation while retaining the legacy single-gateway, drain-first restart path.

**Tech Stack:** Python, macOS launchd/process groups, SQLite cron and session stores, Unix gateway control socket, Telegram polling/token locks, existing `scripts/run_tests.sh` and `./bin/ci` gates.

---

## Baseline and contract

This was written as a **fork plan** before implementation; [Shipped Status](#shipped-status) records what landed. The maintained fork `0xble/hermes-agent` `main` is the implementation base; the runtime installation and source checkout are distinct ([runtime ownership](runtime-ownership.md)). Before S1, `cron/scheduler.py::_launch_external_cron_worker` used `restart_safe_gateway_child_argv`; on non-Linux that resolver returns `in_process` (`tools/process_registry.py`), so a macOS launchd gateway does not get an external restart-safe cron worker. Linux's systemd transient-scope model cannot be copied to macOS: `launchctl bootout` terminates the job process group, but a `start_new_session` child and double-forked `setsid` grandchild can survive outside that group. Design the macOS worker boundary and explicit kill semantics around that topology, without claiming bootout alone reaps all descendants.

The profile-local `cron/executions.py` ledger records attempt owner PID/start fingerprint and immutable terminal states; it is **not a retry queue**. The current cron inactivity watchdog is idle-based, not a hard wall-clock cap. Scheduler tick locking, `pending_slot`/`scheduled_instant` deduplication, profile scope, and delivery ownership remain authoritative ([cron contract](../cron/AGENTS.md)). Current restart/delegation policy can wait or offer explicit parent-driven recovery, but does not provide uninterrupted overlapping execution ([delegation restart](delegation-restart.md)). The gateway's control socket presently supports identity/status/pause-for-update; it is a candidate coordination seam, not yet a handoff protocol. Upstream's open [structured safe restart PR #71876](https://github.com/NousResearch/hermes-agent/pull/71876) addresses agent-request coordination, not evidence that release pinning or two-generation handoff already exists. Recheck upstream and fork source before each implementation slice.

**Global invariants:** Never automatically retry an interrupted execution, chat turn, or delegation. Preserve one admitted owner per event/occurrence and one active Telegram poller per token; require a receipt from the owning process before acknowledging forwarded input. Preserve both gateway busy-message guards, identity/authorization and per-profile secret scope, prompt-cache stability, turn alternation, and existing durable delivery semantics. Shared databases and `~/.hermes/plugins` must remain readable and writable by both `current` and `previous` during rollback; use additive/backward-compatible migrations and dual-version contract tests, never a destructive migration during promotion. Do not infer a live runtime revision from source `HEAD`.

## S1 — macOS cron run survives gateway restart

**Dependency:** None. **Observable result:** A launchd-managed gateway can stop/restart while its active cron run continues, with its one scheduled occurrence still owned by that run. Extend the existing dispatch and handoff path in `tools/process_registry.py`, `cron/scheduler.py`, `cron/scheduler_detached_worker.py`, and relevant `tests/cron/` and macOS-marked process tests. Spawn a session-detached worker outside the launchd job's process group, with pinned profile scope, explicit startup acknowledgement, owner PID/start fingerprint, and durable state before gateway release. Do not equate detachment with an unlimited lifetime or use a bare PID as authority. A separate supervised termination strategy must reach descendants that escape through `setsid`; verify the actual macOS process topology, including double fork, rather than faking `sys.platform`.

**Acceptance:** On macOS, an actual launchd bootout/re-bootstrap during a long active cron worker leaves it running exactly once and able to finish and deliver; no second tick dispatches the occurrence. A failed launch/ack does not silently mark success or lose the claimed slot. Prove profile A→B→A scoping and Linux systemd behavior remain unchanged. Run focused tests with `scripts/run_tests.sh` and a disposable-profile launchd integration test. **Containment:** If a restart-safe worker cannot be established, fail visibly before claiming seamless behavior; keep the current restart/drain path available. This slice does not change installation layout or chat handoff.

## S2 — immutable code and reversible promotion

**Dependency:** S1. **Observable result:** A running worker continues importing the code it started with while a new gateway boots from another release. Install complete releases under `releases/<sha>/`, building each virtual environment at its final release path with a warm dependency cache. Changed dependencies require an isolated compatible environment, never mutation of an environment still used by a live release. Point `current` atomically at the candidate only after staging and verification; retain `previous` and three rollback-capable releases (do not prune a release referenced by a live gateway/worker or recovery receipt). Pin executable, working directory, import path, and release SHA at worker launch so a pointer flip cannot change its imports mid-run. Keep profile state and `~/.hermes/plugins` shared, not duplicated into releases.

Likely surfaces: `hermes_cli/update_cmd*.py`, `hermes_cli/gateway_launchd.py`, install/rollback scripts, runtime receipt tests under `tests/hermes_cli/` and `tests/scripts/`. Reconcile existing updater/snapshot and launchd service contracts rather than adding an independent updater. **Acceptance:** Promote release B while an A worker runs, verify A's actual loaded code SHA and B's gateway SHA separately; flip back to A on a staged candidate failure. Test unchanged/changed dependency paths, atomic-pointer crash points, three-release retention, live pin protection, and a consistent-copy schema rehearsal against both versions. **Containment:** No pointer flip on failed staging; retain the previously healthy release and receipts. No gateway activation follows merely from publishing this plan.

## S3 — bounded workers, delivery, and honest recovery

**Dependency:** S2. **Observable result:** A detached cron run has a hard wall-clock limit and its terminal outcome is recorded correctly whether or not a gateway is up. Introduce a configurable finite hard timeout distinct from the existing inactivity and script limits; terminate the verified worker's whole owned process tree/session, including escaped descendants, then record the observed timeout once. Extend `cron/executions.db` additively with immutable code SHA and execution identity; record delivery independently from model/run completion. The worker must enqueue durable outbound delivery when adapters are unavailable, and the next eligible gateway must deliver through the owning profile without inventing a new run. Reconcile startup ledger rows with live detached workers by verified identity and handoff state: a **live** detached worker stays `running`, never `unknown` merely because its gateway died; a proved-dead owner can transition according to existing immutable terminal rules. Do not replay external side effects, retry the occurrence, or turn ambiguous status into success.

Likely surfaces: `cron/executions.py`, `cron/scheduler.py`, `cron/delivery_queue.py`, worker termination and matching process tests. **Acceptance:** Gateway-down completion queues exactly the eligible delivery and drains it after restart; duplicate drain is suppressed by durable receipt. Live owner survives restart reconciliation without `unknown`; PID reuse and unreadable start-time fingerprint fail closed. Hard timeout kills a `setsid` grandchild and records one failure; a past terminal row stays immutable. Exercise real SQLite/profile boundaries with `scripts/run_tests.sh tests/cron/`, plus macOS detached-process integration. **Containment:** Missing reliable process identity or delivery receipt blocks destructive reconciliation, retains inspectable state, and never triggers automatic rerun.

### S2 settled decisions (implementation contract)

- **Virtual environments are per release and built in place.** Each `releases/<sha>/` owns `.venv`, built at its final path from that release's lockfile (`uv sync --frozen`). Do not physically clone or relocate a venv: shebangs, activation scripts, and metadata can retain the old absolute path. A warm-cache build on this Mac in a disposable source export took 1.60s (`/usr/bin/time -p mise x uv@0.12.13 -- uv sync --frozen --python <base-interpreter>`); this is a measurement, not a service-activation result.
- **Plugins remain shared.** `HERMES_HOME/plugins` is owned and changed independently by agentkit. Promotion does not modify it. Staging copies the relevant plugin tree and config into a disposable profile and imports enabled plugins with the candidate interpreter (without calling `register()`); an import failure aborts before the `current` pointer changes. Agentkit must keep plugin releases compatible with both current and previous Hermes releases; that compatibility contract is outside S2.
- **Promotion order is transactional.** Stage the fetched source, private venv, and plugin smoke; write `previous` to the old target; atomically replace `current`; then retain `current`, `previous`, three rollback-capable releases, and every release pinned by a live process or receipt. A failed stage leaves the old pointer untouched.
- **Runtime pinning.** launchd definitions resolve program, cwd, venv, and import path through `current`; a detached cron worker resolves `current` once at launch and pins the resulting release path for its executable, cwd, and `PYTHONPATH`. `~/.hermes/hermes-agent` remains the git source checkout and is not replaced by a release.
- **Migration and rollback.** The first promotion creates the current-HEAD release and updates the existing launchd plist through `hermes_cli/gateway_launchd.py`; `hermes update --rollback` atomically points `current` at `previous` and uses the existing restart/report/receipt path. A migration rollback points the plist back to the source checkout. Do not activate this migration on the live install without the parent owner's separate authorization.

### G1 Guardian (Opt-In, macOS launchd)

`gateway.guardian.enabled: true` enables the independent one-shot launchd guardian; it is off by default and is not installed or loaded merely by setting the key. On an immutable release with an installed gateway plist, `hermes gateway guardian install` installs its separate launchd job; `hermes gateway guardian status` reports the enable flag, installation and stopped intent; `hermes gateway guardian uninstall` removes the guardian job. Run these only for the intended profile, not as a consequence of landing source code.

A deliberate gateway stop writes `<HERMES_HOME>/gateway-guardian-stopped`; a start clears it before dispatch so failed start attempts do not leave false stopped intent. While the marker exists the guardian does not repair an unloaded gateway. It waits through `updates.release_acknowledgement_timeout_seconds` (default 180 seconds) before judging an unacknowledged release switch, and rejects stale runtime status. With an intact `current` release and matching launchd plist, it can bootstrap an unloaded gateway or roll back a failed switch to a verified `previous` release. It never repairs a corrupt pointer from the source checkout. A nonblocking lock and a cap of three bootstrap/rollback attempts per hour prevent a repair loop; loaded but unhealthy services are left to launchd or operator inspection rather than force-repaired.

Inspect `<HERMES_HOME>/logs/guardian/` for JSON attempt, result, capped and alert receipts, plus `stdout.log` and `stderr.log`. Identical recent alerts are deduplicated and receipts older than an hour are pruned. The guardian does not retry interrupted work.

## Withdrawn: Overlap and Forward-Only Handover (2026-10-06)

The overlap and forward-only gateway handover feature was removed from this fork. In live use on 2026-10-05 it parked the gateway three times: consumed-scope `SystemExit(0)` followed forced or crashed exits, and the startup gate failed under load. No live handover ever completed. The feature also created unnecessary divergence from upstream.

The legacy single-gateway launchd `KeepAlive` path is canonical. Restarts remain drain-first; the gateway uses the existing launchd service and recovery behavior. Existing coordinator database files such as `~/.hermes/gateway-coordinator.db` are ignored.

The former overlap design, generation coordinator, polling transfer, owned routing/admission, startup gate, and forward-only updater are withdrawn rather than partially supported. The shipped Phase 0, S1–S3, durable outbox, immutable-release, and G1 guardian behavior remains subject to the legacy single-gateway path.

The Telegram review findings about losing the durable `getUpdates` journal and releasing the token lock before `updater.stop()` apply only to the flag-on controlled poller. That poller created `_controlled_journal` and used token-lock fencing only when `overlap_handover_enabled(...)` was true. With the flag off—the shipped default—the legacy PTB updater path is unchanged: its offset-confirmation window is pre-existing upstream behavior, and the legacy updater releases the token lock early for bounded teardown. No live legacy-path behavior was removed here.

### Leftovers after withdrawal

Profiles where forward-only handover ran may retain `gateway-coordinator.db`, `ai.hermes.gateway.g-<uuid>` LaunchAgents/labels, `forward-update.json`, `gateway_runtime.<gen>.json`, and per-PID heartbeat files. Check for generation labels manually with:

```sh
launchctl list | grep ai.hermes.gateway.g-
```

`HERMES_GENERATION_SCOPE` in an existing plist is inert after withdrawal and disappears the next time `hermes gateway install` rewrites the plist. No automatic cleanup is performed.

## Shipped Status

Every row merged with an independent `review_candidate` approval on its exact head and a green exact-SHA `qualification` check, using normal merges.

| Slice | PR | Merge |
|---|---|---|
| Phase 0: Telegram status per topic/turn | [#186](https://github.com/0xble/hermes-agent/pull/186) | `ec3bac417f` |
| Phase 0: restart follow-ups, bounded notices | [#188](https://github.com/0xble/hermes-agent/pull/188) | `396382f4ac` |
| S1: macOS cron survives gateway restart | [#187](https://github.com/0xble/hermes-agent/pull/187) | `8649c8e7ef` |
| S3: bounded workers, durable delivery | [#194](https://github.com/0xble/hermes-agent/pull/194) | `a79fbaa6d8` |
| Synthetic reply anchors (restart replay) | [#202](https://github.com/0xble/hermes-agent/pull/202) | `ceabe38a5a` |
| S2: immutable releases and rollback | [#192](https://github.com/0xble/hermes-agent/pull/192) | `d64abc4778` |
| S2: one reload per release switch | [#211](https://github.com/0xble/hermes-agent/pull/211) | `784396c5ec` |
| S4 design and 7-day baseline | [#213](https://github.com/0xble/hermes-agent/pull/213) | `2748219c1e` |
| H1: update receipt order, pointer, config | [#217](https://github.com/0xble/hermes-agent/pull/217) | `6e7f26c95c` |
| H1: manual cron outcome and delivery | [#216](https://github.com/0xble/hermes-agent/pull/216) | `24bb7b9256` |
| H1: bounded manual-run recovery | [#221](https://github.com/0xble/hermes-agent/pull/221) | `f368d1e772` |
| S4.1: durable outbox and admissions | [#218](https://github.com/0xble/hermes-agent/pull/218) | `6b2138ed2e` |
| G1: guardian launchd job | [#219](https://github.com/0xble/hermes-agent/pull/219) | `d6e1738b37` |
| G1: grace from config, stale status | [#225](https://github.com/0xble/hermes-agent/pull/225) | `c3ab78c94d` |
| S4.1: egress off loop, retention | [#226](https://github.com/0xble/hermes-agent/pull/226) | `fbef2e6be3` |
| H1: update config errors | [#220](https://github.com/0xble/hermes-agent/pull/220) | `b633a3532c` |
| H1: resumed turn reply attribution | [#214](https://github.com/0xble/hermes-agent/pull/214) | `96f6a8eb2f` |
| Part 2 review follow-ups | [#228](https://github.com/0xble/hermes-agent/pull/228) | `f7d27da20d` |
| Forward-only amendment | [#269](https://github.com/0xble/hermes-agent/pull/269) | `4f40197eba` (withdrawn 2026-10-06) |
| Forward-only cold start | [#275](https://github.com/0xble/hermes-agent/pull/275) | `8841a62478` (withdrawn 2026-10-06) |
| Forward-only promotion, rollback | [#276](https://github.com/0xble/hermes-agent/pull/276) | `942ffd6f28` (withdrawn 2026-10-06) |
| Native forward-only acceptance | [#282](https://github.com/0xble/hermes-agent/pull/282) | `60d389afb3` (withdrawn 2026-10-06) |

Drafts [#215](https://github.com/0xble/hermes-agent/pull/215) (S4.2 spike) and [#224](https://github.com/0xble/hermes-agent/pull/224) (S4.2 rerun spike) were closed unmerged.

S3 landed before S2 (its code-SHA ledger column works without releases). S2 is **opt-in** with `updates.immutable_releases: true` and macOS launchd only; its runbook, state table and qualification contract live in [seamless-restart-s2.md](seamless-restart-s2.md). One shared observe-only wait (`wait_for_release_acknowledgement`, `updates.release_acknowledgement_timeout_seconds`, default 180) completes every switch, rollback, pending-switch finish and service repair only after the new gateway acknowledges from the intended release. Each switch reloads launchd exactly once: the fleet step credits the acknowledged gateway instead of relaunching it. Disposable rehearsals with real launchd jobs and every `launchctl` call captured proved first migration, release→release, rollback, first-migration rollback, timeout recovery, repeated rollback and repair.

**Live on this Mac (2026-09-27).** Config: `platforms.telegram.extra.drop_pending_on_cold_boot: false`, `platforms.telegram.gateway_restart_notification: false`, `updates.immutable_releases: true`. Promotion took two updates: the pre-S2 updater pulled `784396c5ec` into the source checkout, then the S2 updater's no-pull reconciliation performed the first migration (receipt `update_20260927_151424_22786.json`): `current` → `releases/784396c5…`, `previous` → the source checkout, launchd `ProgramArguments`/`WorkingDirectory` through `current`, one reload, gateway acknowledgement recorded in `release-last-txn.json`. A controlled restart afterwards showed: a 240 s cron probe started before the restart kept running under its pinned release interpreter and completed once with `code_sha` and one delivery; a Telegram message sent while the old gateway was down was answered after boot; a follow-up queued behind an active turn was preserved and replayed; per-chat and home-channel notices were suppressed; teardown took 2.3 s against `ExitTimeOut` 60 with the pending-message flush recovered on boot.

**Part 2 live on this Mac (2026-09-27/28).**
- **Promotion to `96f6a8eb2f`:** ran `hermes update` from 23:53 to 23:55 PT.
  - The receipt is `update_20260927_235526_59534.json`.
  - Its steps, in order: `pre_update_backup`, `immutable_maintenance`, `immutable_release`, `immutable_activation`, `release_retention`, and outcome `success`.
  - `current` points to `releases/96f6a8eb…` and `previous` to `releases/ed54722316…`. The gateway acknowledged from the new release.
  - A pinned cron worker started under `ed54722` kept running through the reload and completed once.
  - One session interrupted by the reload was auto-resumed, and its answer was delivered to its original topic.
- **Outbox:** `gateway.durable_outbox.enabled: true` has been live since 23:57. `gateway-outbox.db` records inbound admissions and outbound sends. The first reply recorded one attempt, the Telegram message id, `delivered`, and no duplicate idempotency key.
- **Second promotion, to `f7d27da20d` (#228):** ran 00:51–00:53 PT on 2026-09-28. `current` points to `releases/f7d27da…` and `previous` to `releases/96f6a8eb…`, so both retained releases contain G1.
- **Guardian:** `gateway.guardian.enabled: true`. Installed with the release CLI (`~/.hermes/current/.venv/bin/hermes gateway guardian install`), not the source-checkout `hermes` shim, which predates G1.
  - `ai.hermes.gateway-guardian` is loaded in `gui/501` with `StartInterval` 30 and `RunAtLoad`, and pins `current/.venv/bin/python`.
  - Its first seven runs each printed `healthy`, exited 0, and wrote no stderr and no repair receipts.
  - The item-7 E2E ran at `f7d27da20d` against real launchd with disposable labels (`test_gateway_guardian_launchd_real.py`, 36 passed together with the guardian unit tests):
    - an unloaded service was re-bootstrapped within 60 s;
    - the stop marker was honoured;
    - an unacknowledged switch rolled back to the verified `previous` release;
    - no labels were left behind.
  - The production gateway was not deliberately unloaded, because doing that would cut every in-flight turn to prove a path the disposable run already covers.
- **H1 evidence:**
  - Receipt ordering and resumed-reply attribution were observed live.
  - The pointer and config validation were reproduced in a disposable `HERMES_HOME` with the release interpreter.
  - The error-surface, manual-cron, double-fork and commit/enqueue-gap items are covered by named regression tests: 30 passed at `96f6a8eb2f`. No live trigger occurred for them.

**Remaining risks.**

- **Restarts stay drain-first.** An in-flight chat turn or background delegation running during a restart or update is still cut, because `restart_drain_timeout: 0`.
  - A cut chat turn auto-resumes with a restart note, and its answer lands in the original topic. The interrupted tool call is not retried.
  - Detached cron workers are not cut, because they are pinned to their release.
  - The withdrawn overlap/forward-only handover is not a supported path.
- **Send outcome is uncertain after a crash.** The outbox holds any send whose outcome is unknown and never resends it. A crash between the platform accepting a message and the receipt commit can therefore leave a reply unconfirmed in the outbox while the user did receive it. A refused connection is treated as definitely unsent. Missing platform ids get random ids, and a multi-part send shares one record.
  - An adapter refusal made before any request (Telegram `Not connected` or `send_path_degraded`) carries `SendResult.pre_send` and records `failed_unsent`, so the same payload may be dispatched again. A final refused this way is handed to the delivery ledger, whose reconnect sweep redelivers it.
  - An outbox hold carries `SendResult.held`. `_send_with_retry` returns it as final: it never retries and never sends the "Response formatting failed" plain-text copy, which would get past the duplicate guard as a new payload.
- **The guardian never force-repairs.** It leaves a loaded-but-unhealthy gateway alone, stops after three repairs per hour, and treats a wedged heartbeat older than 120 s as unhealthy. It never repairs a corrupt `current` pointer from the source checkout.
- **Some hardening paths have only test evidence.** The manual-run kill, double-fork sweep and commit/enqueue-gap paths have regression tests but no live trigger yet.
- **The full unscoped update path can only be proven live.** Disposable rehearsals cover it with throwaway launchd labels.
