# Seamless Restart Phase 1 Implementation Plan

Patch identity: `seamless-restart`.

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

## Local Admission Replay

An already admitted event may revisit the native adapter during startup, restore,
or deferred execution. Its local admission is reusable only for the same event
object, runner, owning home/profile, transport identity, and routed session. A new
transport event remains a durable duplicate. Startup and restore gates retain
the local owned-admission marker until actual dispatch. Verify the native adapter
and runner path with `tests/gateway/test_durable_outbox.py` and
`tests/gateway/test_owned_routing.py`, including profile A to B to A.


> **Status (2026-09-28):** S1–S3, H1, S4.1 durable outbox and G1 guardian shipped on fork `main`; the live gateway still restarts drain-first. The S4.2 router/executor split stopped after two failed spikes and was not built. [Overlap Handover](#overlap-handover) is a replacement **design**, not implemented or enabled. Later changes follow repository policy, verification, review and separate runtime-activation authority.

**Goal:** Promote a new Hermes gateway release without interrupting active cron executions or in-flight conversations, while making failure and rollback observable.

**Architecture:** First detach macOS cron workers from the launchd gateway job and pin their code, then install immutable releases behind an atomic `current` pointer. Add bounded worker termination and truthful ledger reconciliation before designing a two-complete-gateway overlap. One generation admits new work; the previous generation owns only work already in flight.

**Tech Stack:** Python, macOS launchd/process groups, SQLite cron and session stores, Unix gateway control socket, Telegram polling/token locks, existing `scripts/run_tests.sh` and `./bin/ci` gates.

---

## Baseline and contract

This was written as a **fork plan** before implementation; [Shipped Status](#shipped-status) records what landed. The maintained fork `0xble/hermes-agent` `main` is the implementation base; the runtime installation and source checkout are distinct ([runtime ownership](runtime-ownership.md)). Before S1, `cron/scheduler.py::_launch_external_cron_worker` used `restart_safe_gateway_child_argv`; on non-Linux that resolver returns `in_process` (`tools/process_registry.py`), so a macOS launchd gateway does not get an external restart-safe cron worker. Linux's systemd transient-scope model cannot be copied to macOS: `launchctl bootout` terminates the job process group, but a `start_new_session` child and double-forked `setsid` grandchild can survive outside that group. Design the macOS worker boundary and explicit kill semantics around that topology, without claiming bootout alone reaps all descendants.

The profile-local `cron/executions.py` ledger records attempt owner PID/start fingerprint and immutable terminal states; it is **not a retry queue**. The current cron inactivity watchdog is idle-based, not a hard wall-clock cap. Scheduler tick locking, `pending_slot`/`scheduled_instant` deduplication, profile scope, and delivery ownership remain authoritative ([cron contract](../cron/AGENTS.md)). Current restart/delegation policy can wait or offer explicit parent-driven recovery, but does not provide uninterrupted overlapping execution ([delegation restart](delegation-restart.md)). The gateway's control socket presently supports identity/status/pause-for-update; it is a candidate coordination seam, not yet a handoff protocol. Upstream's open [structured safe restart PR #71876](https://github.com/NousResearch/hermes-agent/pull/71876) addresses agent-request coordination, not evidence that release pinning or two-generation handoff already exists. Recheck upstream and fork source before each implementation slice.

**Global invariants:** Never automatically retry an interrupted execution, chat turn, or delegation. Preserve one admitted owner per event/occurrence and one active Telegram poller per token; require a receipt from the owning process before acknowledging forwarded input. Preserve both gateway busy-message guards, identity/authorization and per-profile secret scope, prompt-cache stability, turn alternation, and existing durable delivery semantics. Shared databases and `~/.hermes/plugins` must remain readable and writable by both `current` and `previous` during overlap or rollback; use additive/backward-compatible migrations and dual-version contract tests, never a destructive migration during promotion. Do not infer a live runtime revision from source `HEAD`.

## Deadline contract

Every bounded startup, handover, takeover, rollback, guardian-repair, wedge-proof, poll-proof, and bootout operation follows these rules:

1. Give every blocking subprocess, socket, psutil wait, and SQLite busy wait no more than the remaining budget.
2. Reject success observed at or after expiry.
3. Re-check the deadline inside each irreversible transaction after its write lock is acquired; roll back when expired.
4. After lease/pointer commit, lateness is a typed committed outcome or commit-clocked poll, never a plain failure or rollback of a healthy successor.
5. Exception-path recovery uses the remaining budget or a named short reserve, never a fresh full interval. The one named exception is `recover_forward(late=True)` after the original rollback bound has expired: it records the missed original bound (`late_rollback.bound_missed=true`) and receives a fresh `ROLLBACK_SECONDS` operating budget, with an alert.
6. The old owner's cooperative wire stop during `transfer_requested` is bounded by the poller's own long-poll limit (`timeout+1`), not the driver's window. The driver's window is protected by its socket timeout, and the owner self-rearms under the same lease and nonce.
7. Re-observing an already-committed holder after its durable proof window is gone (expired, other boot, or never recorded) uses the named `POLL_SECONDS` observation reserve, min-ed with any enclosing scope, and is marked `proof_window=reobservation`; it never counts as proof inside the original bound.

**Scope.** The contract covers the forward-only overlap paths (`gateway_forward_update`, `run_generation`, `generation`, guardian, controlled poller). Inside them, `activate_release` runs only within a `_flip_scope` and after an explicit expiry check; it is a local fsync'd pointer transaction with no wait in that path. The legacy non-overlap update and `repair-service` callers of `activate_release` (`update_cmd.py`, `immutable_releases.promote`/`rollback`) predate this design, are unchanged here and are not covered; bounding them is a follow-up, not a precondition of the forward-only flag.

## S1 — macOS cron run survives gateway restart

**Dependency:** None. **Observable result:** A launchd-managed gateway can stop/restart while its active cron run continues, with its one scheduled occurrence still owned by that run. Extend the existing dispatch and handoff path in `tools/process_registry.py`, `cron/scheduler.py`, `cron/scheduler_detached_worker.py`, and relevant `tests/cron/` and macOS-marked process tests. Spawn a session-detached worker outside the launchd job's process group, with pinned profile scope, explicit startup acknowledgement, owner PID/start fingerprint, and durable state before gateway release. Do not equate detachment with an unlimited lifetime or use a bare PID as authority. A separate supervised termination strategy must reach descendants that escape through `setsid`; verify the actual macOS process topology, including double fork, rather than faking `sys.platform`.

**Acceptance:** On macOS, an actual launchd bootout/re-bootstrap during a long active cron worker leaves it running exactly once and able to finish and deliver; no second tick dispatches the occurrence. A failed launch/ack does not silently mark success or lose the claimed slot. Prove profile A→B→A scoping and Linux systemd behavior remain unchanged. Run focused tests with `scripts/run_tests.sh` and a disposable-profile launchd integration test. **Containment:** If a restart-safe worker cannot be established, fail visibly before claiming seamless behavior; keep the current restart/drain path available. This slice does not change installation layout or chat handoff.

## S2 — immutable code and reversible promotion

**Dependency:** S1. **Observable result:** A running worker continues importing the code it started with while a new gateway boots from another release. Install complete releases under `releases/<sha>/`, building each virtual environment at its final release path with a warm dependency cache. Changed dependencies require an isolated compatible environment, never mutation of an environment still used by a live release. Point `current` atomically at the candidate only after staging and verification; retain `previous` and three rollback-capable releases (do not prune a release referenced by a live gateway/worker or recovery receipt). Pin executable, working directory, import path, and release SHA at worker launch so a pointer flip cannot change its imports mid-run. Keep profile state and `~/.hermes/plugins` shared, not duplicated into releases.

Likely surfaces: `hermes_cli/update_cmd*.py`, `hermes_cli/gateway_launchd.py`, install/rollback scripts, runtime receipt tests under `tests/hermes_cli/` and `tests/scripts/`. Reconcile existing updater/snapshot and launchd service contracts rather than adding an independent updater. **Acceptance:** Promote release B while an A worker runs, verify A's actual loaded code SHA and B's gateway SHA separately; flip back to A on a staged candidate failure. Test unchanged/changed dependency paths, atomic-pointer crash points, three-release retention, live pin protection, and a consistent-copy schema rehearsal against both versions. **Containment:** No pointer flip on failed staging; retain the previously healthy release and receipts. No gateway activation follows merely from publishing this plan.

## S3 — bounded workers, delivery, and honest recovery

**Dependency:** S2. **Observable result:** A detached cron run has a hard wall-clock limit and its terminal outcome is recorded correctly whether or not a gateway is up. Introduce a configurable finite hard timeout distinct from the existing inactivity and script limits; terminate the verified worker's whole owned process tree/session, including escaped descendants, then record the observed timeout once. Extend `cron/executions.db` additively with immutable code SHA and execution identity; record delivery independently from model/run completion. The worker must enqueue durable outbound delivery when adapters are unavailable, and the next eligible gateway must deliver through the owning profile without inventing a new run. Reconcile startup ledger rows with live detached workers by verified identity and handoff state: a **live** detached worker stays `running`, never `unknown` merely because its gateway died; a proved-dead owner can transition according to existing immutable terminal rules. Do not replay external side effects, retry the occurrence, or turn ambiguous status into success.

Likely surfaces: `cron/executions.py`, `cron/scheduler.py`, `cron/delivery_queue.py`, worker termination and matching process tests. **Acceptance:** Gateway-down completion queues exactly the eligible delivery and drains it after restart; duplicate drain is suppressed by durable receipt. Live owner survives restart reconciliation without `unknown`; PID reuse and unreadable start-time fingerprint fail closed. Hard timeout kills a `setsid` grandchild and records one failure; a past terminal row stays immutable. Exercise real SQLite/profile boundaries with `scripts/run_tests.sh tests/cron/`, plus macOS detached-process integration. **Containment:** Missing reliable process identity or delivery receipt blocks destructive reconciliation, retains inspectable state, and never triggers automatic rerun.

## Overlap Handover

**Status: design only, not enabled.** S1–S3, S4.1 and G1 remain shipped; the router/executor extraction is abandoned. The 2026-09-26 03:47 to 2026-09-28 10:55 baseline supplied for this redesign counted 31 gateway stops, 30 with work in flight, and 149 turns, 18 cron runs and 104 background delegations cut, followed by 211 recovery resumes across 55 sessions. Treat these as observational counts, not proof each stop caused every cut. The replacement is two *complete* release-pinned gateways, A and B. A keeps its agent cache, active turns, pending approvals, async delegations, tool processes, adapter send/edit capability and background watchers until its own work ends. B takes new admission and the sole Telegram polling lease. Neither a two-hour cap nor the outbox can promise unconditional exactly-once Telegram delivery after a transport-success/receipt crash. The overlap is feasible **conditionally**, not yet proven end to end: current singleton PID/control/status paths, PTB's offset handling and the updater/guardian must change behind a default-off flag. A bare second `gateway run` cannot satisfy this design.

### Forward-Only Amendment (2026-09-30)

This amendment governs the whole handover design. No generation regains polling or admission after its lease moves. The sections below have been aligned to it: rollback in the crash outcomes, guardian recovery and the slice 4 proof all start a fresh generation on `previous`.

**Why.** The rollback that re-armed draining A in place failed the native launchd acceptance test on 2026-09-30 in both the reviewer's and the parent's runs (`test_launchd_guardian_rolls_back_keepalive_successor_failure`). The guardian recorded `rolled_back`, the lease returned to A with a new epoch, exactly one poller ran, and A durably accepted fresh update 4002. A never replied. Six consecutive review rounds on #266 found defects on that path: nonce binding, a dead successor stuck in `draining`, legacy fleet reloads after promotion, unflagged restore failures, a lost drain deadline, and the final dispatch gap. Each fix added state to a process that had already begun to wind down: stopped pollers, the draining flag, drain tasks, transfer receipts and session claims. The amendment removes that path rather than repairing it again.

**Rule.** A generation that has started draining never serves again.

States, per generation, are forward only:

1. `standby`: process up, no poller, no user-facing admission, no cron or kanban dispatch. The only turn it may run is the startup-gate loopback turn below, which never touches a user session or Telegram.
2. `serving`: holds the `active_generation` lease, polls Telegram, admits new work.
3. `draining`: lease gone. Finishes its own turns, approvals, delegations, watchers and outbox rows through its own Bot API client. Admits nothing new.
4. `exited`: process gone after its last obligation, or at the two-hour cap.

`failed` remains a coordinator verdict on a dead or refused generation, not a fifth runtime state. It is stored in the separate, never-cleared `generations.verdict` column, so `state` holds only the four runtime states. A generation in `standby` that never serves goes straight to `exited`.

The lease moves in exactly two ways:

1. **Handover.** Cooperative only. A serving generation stops its poller, acknowledges `poller_stopped(token, epoch, cursor)` and commits the lease to a ready standby. The old generation becomes `draining` in the same transaction. A generation that does not acknowledge within its deadline has not handed over, and still owns the lease.
2. **Takeover.** A ready standby takes the lease from a generation that is proven dead and retired, in this order:
   1. Death proof: the recorded PID plus start fingerprint is absent. Heartbeat age alone never proves death.
   2. Retire: in one transaction the coordinator sets the durable `verdict='failed'` with its `verdict_at` and death evidence, then `state='exited'`. The verdict column is never cleared, so readback proves the generation was retired as failed and not drained cleanly. A launchd KeepAlive respawn under a generation label is never that generation, because its PID and start fingerprint differ. It loses the claim compare-and-swap described under Labels, exits 0, and `KeepAlive={SuccessfulExit=false}` does not respawn it again. It never polls, admits or claims the token, so a respawn cannot race the takeover.
   3. Bootout: the label is booted out, then read back as not loaded. Booting out a dead or retired generation kills no work.
   4. Takeover: the standby acquires the token lock and the lease with a new epoch.

   If a respawn is still live under the retired label when bootout is due, it holds no lease and no token, so booting it out is safe.

The one allowed resume is before commit. A serving generation that paused for a handover that never committed resumes. It never set `draining` and never lost the lease. The pre-commit pause fences three things: the `getUpdates` poller, new cron and kanban dispatch, and internal autonomous wakeups. The abort re-enables all three in one step, then reads each back as armed before it counts as resumed. User-facing admission of already-polled updates was never fenced. This is the existing 45-second pre-commit abort. The caller reserves up to 2 seconds (one fifth for shorter budgets) inside its unchanged deadline for the abort notification; stopping the wire cannot spend that reserve. Rollback still caps cooperative handover at 10 seconds inside the original 60-second bound. A normal outstanding Telegram long poll can outlive that cap. If the caller aborts while stop is outstanding, the still-serving owner self-rearms as soon as stop completes, after a fresh read proves its process identity, active lease epoch and exact aborted nonce. A stale nonce, replacement or committed/draining owner cannot re-arm. Its native test checks that a fresh message, a fresh cron tick, a kanban claim and a goal wakeup each run after the abort.

**Rollback is a handover.** Rolling back means starting a fresh standby on the previous release, which then takes the lease by cooperative handover (unhealthy but responsive successor) or takeover (dead successor). The draining generation, if any, keeps draining and exits on its own schedule. Three generations can therefore coexist briefly: the original draining A, the failed B (draining or dead), and the fresh A′ on A's release. `previous → current` changes only after A′ acknowledges from its release. Never re-arm the draining A.

**Labels.** The alternating `-a` and `-b` labels cannot hold three generations. Each generation gets its own label, `ai.hermes.gateway.g-<uuid>`, carrying the full 32-hex generation UUID. The generation row, with that label unique among non-exited rows in the coordinator (the partial unique index in the schema below), is inserted before any launchd action, so a colliding live reservation fails the insert and never reaches launchd. Exited rows stay as history and never block their label. The process writes its PID and start fingerprint into that row at its first coordinator check, before it can claim anything, so bootstrap, bootout and respawn fencing always address exactly one generation. The label pins its release path and is booted out only after the coordinator records `exited` for that generation, whether by clean drain or by the retire step of takeover. The legacy `ai.hermes.gateway` label remains the first A during migration and keeps its name until it exits.

**Claim.** The row is inserted with `pid` and `start_fingerprint` null, `state='standby'` and no verdict. The first process launched under the label claims it with one compare-and-swap: `UPDATE generations SET pid=?, start_fingerprint=?, boot_id=?, scope_nonce=? WHERE id=? AND pid IS NULL AND state='standby' AND verdict IS NULL`. Exactly one process can win. Every process that loses exits 0 before touching any lease, token or transport, unless the claim-scope rules below let it replace a dead claimant from an earlier scope.
- **Crash before claim:** nothing has been claimed, so a respawn that wins the claim is simply the generation's first process. It starts standby from the beginning and must still pass the startup gate.
- **Crash after claim:** a respawn loses the claim and exits 0. The recorded PID and fingerprint belong only to the claimant, so the takeover death proof tests the right process.
- **Never claimed:** if no process claims the row before the standby's 45-second startup deadline, the updater sets `verdict='failed'` with the evidence `unclaimed` and `state='exited'`, then boots the label out. An unclaimed generation holds no lease, so this is always safe.

**Cold start (2026-09-30 addendum).** A cold start has no live generation to hand over from: after a reboot, after a crash that nothing replaced, or after `hermes gateway stop` then `start`. It needs no new state and no third lease move. It is a takeover from a holder that is proven dead or that exited cleanly.

- **Claim scope.** A claim is valid for one supervisor bootstrap, not for the label's lifetime. The scope is the pair (boot ID, bootstrap nonce). Each time the launcher (updater, guardian or `hermes gateway start`) bootstraps the label, it generates a fresh random nonce and writes it into the plist environment as `HERMES_GENERATION_SCOPE`. launchd passes the same environment to every KeepAlive respawn of that bootstrap. After a reboot launchd reloads the same plist, so the nonce repeats but the boot ID differs, which makes it a new scope. The claim stores the scope durably: the claim compare-and-swap also sets `scope_nonce=?` and `boot_id=?` from the claiming process. A scope is single-use. Once any row for a label, live or exited, records a scope, that scope can never claim or reserve again for that label.
- **Service label.** Only one label may create a generation row for itself: the label of the generation named by the `active_generation` lease row, whether the lease is held or released. When no lease row exists yet, it is the legacy `ai.hermes.gateway` label. Any other label, such as a completed drainer, a retired standby or a failed successor, never reserves a row for itself. Its only path to running is a row the updater reserved before bootstrapping it.
- **Rules.** A starting process reads its own boot ID and `HERMES_GENERATION_SCOPE` and applies the first matching rule:
  1. **Consumed scope.** Any row for this label, live or exited, has the same `boot_id` and `scope_nonce`. This is a KeepAlive respawn of a bootstrap that has already claimed. It exits 0, whether the claimant is alive, dead or retired. This rule closes the window in which takeover has retired a dead claimant but has not yet booted its label out.
  2. **Unclaimed row** (`pid IS NULL`, not exited). Claim it with the compare-and-swap above, recording this scope. This covers an updater-reserved standby and the crash-before-claim case.
  3. **Claimed row, different scope.** If the claimant is alive by PID plus start fingerprint, exit 0 without changing anything. If the claimant is dead and this is the service label, retire the row with `verdict='failed'` and evidence `boot_changed` or `dead`. In the same transaction, reserve and claim a fresh row with this scope. If the claimant is dead and this is not the service label, retire the row and exit 0.
  4. **No non-exited row.** If this is the service label, reserve and claim a fresh row with this scope. This covers a first start, a reboot and a start after a clean stop. Otherwise, exit 0.

  The partial unique index never sees two live rows for one label. A scoped forward-only `gateway run` bypasses the generic host-attach guard so a consumed KeepAlive respawn reaches this coordinator claim and exits 0. Otherwise a live successor's host record would cause exit 75 and an endless respawn loop. Unscoped and flag-off launches retain host attachment.

  A process with no `HERMES_GENERATION_SCOPE` (a direct CLI launch, or a plist written before this addendum) uses a per-process random nonce, so it is always a new scope and can only replace a dead claimant. A row whose `scope_nonce` is null was claimed before scopes existed and is treated as a different scope. A fresh row from rule 3 or 4 starts in `standby` like any other generation. It must pass the startup gate, and it gains the lease only by the two lease moves, so replacing a dead claimant never by itself makes a poller.
- **Lease.** The fresh generation passes its startup gate, then takes the lease by takeover. If the holder exited cleanly, the lease is already released and the takeover needs no retire step. If the holder is dead, the takeover retires it first. If the dead holder ran under the taker's own label, the bootout step is skipped, because that label's only process is the taker itself. Takeover is never allowed from a live generation, a suspect one, or a generation that neither exited nor is proven dead.
- **Recovery inside one boot.** A crash leaves the label parked: launchd keeps the job loaded, but its respawn lost the claim and exited 0, so no process runs. Today's guardian cannot see this, because `_launch_state` reports any job that `launchctl print` finds as `loaded`, and `_run` returns `waiting` for a loaded, unhealthy job with no pending switch. The guardian therefore gains one repair, built in the runtime slice. It reads the job's `launchctl print` state and PID, and treats a loaded job with no running PID and a last exit status of 0 as parked. It repairs only the service label defined above. Another generation must not be serving. The one exception is a generation that is this label's own claimant and is proven dead by PID plus start fingerprint, or by a changed boot ID. A crashed holder is exactly this case: its row still says `serving` and its lease is still held, because nothing ran after the SIGKILL. In that case the guardian first retires the dead claimant under the coordinator's transaction lock, with `verdict='failed'`, evidence `dead` or `boot_changed`, and `state='exited'`. The lease row stays as it is, held by an exited and proven-dead generation, which is exactly what takeover requires. If any other generation is serving, or the holder is alive, or its identity cannot be established, the guardian does nothing and reports `waiting`. Any other parked label, such as a completed drainer, a retired standby or a failed successor, is never re-bootstrapped. Once its row is `exited` the guardian only boots it out as cleanup, and that cleanup does not count as a repair. For the service label, once the claimant is retired or has exited cleanly, the guardian boots the label out and reads it back as unloaded. Finally it bootstraps the label again with a fresh nonce, which opens a new scope. The new generation passes its startup gate and takes the lease by takeover. A loaded job with a running PID is never treated as parked. The repair counts against the existing three-repairs-per-hour cap. During an update, the updater starts A′ instead.

**Flag and previous release.** Forward-only handover is enabled by its own key, `gateway.forward_only_handover.enabled`, default off. Releases from before this amendment, including `738c502c`, read only `gateway.overlap_handover.enabled`. That key stays false for as long as any pre-amendment release can run on the profile, including as `previous`. A forward-only release refuses to start with its key on while `gateway.overlap_handover.enabled` is true, and the updater refuses to activate or stage one in that configuration. With the legacy key false, pre-amendment releases take their flag-off path, which does not write the `generations` table, so they never run their in-place overlap code against a forward-only coordinator. The updater uses the overlap path only when both the serving release and the target release are forward-only-capable. Otherwise it uses the existing single-gateway path. So the first forward-only release is installed by one ordinary restart, and A′ is always a forward-only release.

**Startup gate.** Before a standby may be named ready, it runs one loopback turn. This is the single, explicit exception to standby's no-admission rule. A synthetic message from a reserved loopback identity enters the same admission code on an isolated loopback transport, goes through both busy guards and the runner, and produces a reply row in the outbox with a loopback destination. It runs in a reserved loopback session that no user session, cron job or goal can route to. The turn runs with an empty toolset, so the model can only answer in text and cannot call tools, spawn delegations or processes, or write memory or shared state beyond the session and outbox rows. The loopback transport has no network egress. Its reply row is created already terminal with a `synthetic` disposition in the same transaction, so outbox recovery and retry never select it, and no adapter can send it after promotion. The turn uses the configured model with a fixed short prompt, which costs one small model call per update. A standby that fails the gate or exceeds its 45-second deadline never takes the poller. Its own process exits, or the updater stops it after proving it holds no lease. The coordinator then sets the durable `verdict='failed'` and `state='exited'`, and only after that is its label booted out. This check would have caught the 2026-09-30 failure class: a gateway that polls and accepts input but never reaches the runner.

**Crash outcomes under the amendment.**

- **Updater dies before commit.** The serving generation's pre-commit deadline resumes its poller. The standby stays unready until booted out by the next update or the guardian.
- **Updater dies after commit.** The new generation serves. The updater is observer-only on recovery, and the receipt is completed from the coordinator.
- **New generation fails its startup gate.** It never polls. The old generation keeps serving and is never paused.
- **New generation dies after takeover or handover.** The guardian or updater starts A′ on the previous release, which passes its startup gate and takes the lease from the dead generation through the ordered death proof, retire, bootout and takeover steps. A KeepAlive respawn of the dead label exits 0 at its coordinator check. The target is a fresh message answered within 60 seconds of death. Work in the dead generation is marked interrupted once and never replayed.
- **New generation is live but unhealthy after commit.** A′ is started. If the unhealthy generation still responds, it hands over cooperatively and drains or hits its cap. If its event loop is provably wedged by the existing liveness probe (`probe_gateway_loop_liveness`), the updater or guardian terminates it with the existing bounded SIGTERM then SIGKILL path, and the dead-generation case above applies. The rollback path reads the successor's own per-PID heartbeat copy, because the draining generation keeps rewriting the shared file. It requires a heartbeat older than 35 s plus the sustained silent tick-socket witness. A wedge that begins soon after commit can therefore be proved and replaced inside the 60 s bound. A later wedge cannot age into proof in time: the update records blocked, and guardian recovery replaces the proven-wedged generation after the bound, recording the miss and alerting. Other callers keep the 90 s default. A KeepAlive respawn of a dead generation label reaches its coordinator claim before any host-attach check, so a consumed scope exits 0 and parks. A live generation that neither acknowledges handover nor proves wedged keeps the lease and its poller. The outcome is recorded as blocked with an alert, never a second poller and never a forced lease move. This case has its own native test.
- **Old draining generation is SIGKILLed.** Its in-process work is marked interrupted once after death proof. The serving generation is unaffected.
- **Guardian dies.** The next scheduled run recomputes from the coordinator. No action depends on the guardian's in-memory state.

If A′ cannot start and pass its gate within 60 seconds on the live profile, stop and decide between a longer rollback bound and a warm standby. A warm standby is a pre-started `standby` generation on the previous release. It fits the four states but costs a resident process.

**What this removes.** `restore_after_rollback`, the rollback re-arm and drain-fence serialization, `restore_successor_after_failed_rollback`, the `rollback_overlap` branches that return the lease to a prior generation, and the rollback-attention branches that exist only for those paths. The generation isolation, polling journal and cursor, owned admission and routing, drain, cap, fleet fences and guardian observation from #239, #243, #258, #261, #265 and #266 stay.

**Simplicity limit.** At most four runtime states and two lease moves. Adding either requires an amendment here first.

**Acceptance.** One native launchd suite in a disposable profile, run on the exact PR commit, passes three consecutive runs. It covers:

- an in-flight turn, a delegation and an approval each finishing exactly once on the old generation;
- a queued follow-up running;
- at most one poller at any instant, proved from durable evidence rather than sampling: every token-lock acquire and release and every poller start and stop is journaled with its generation and epoch, and the assertion checks that no two poller intervals for a token overlap. Any zero-poller window is bounded and the confirmed cursor is continuous across it (no lost or double-admitted update);
- the new release answering a fresh message;
- the old generation exiting after its last work;
- a release failing its startup gate never polling;
- the startup gate calling no tool and leaving its reply row terminal and unsent through a later promotion and outbox recovery;
- a release dying after commit, with its KeepAlive respawn losing the claim compare-and-swap and exiting 0, and A′ answering a fresh message within 60 seconds;
- a standby crashing before its claim (the respawn claims and must pass the gate) and a standby that never claims (retired as `unclaimed`);
- cold start after a clean stop, after a crash inside one boot (guardian bootstraps a new scope) and after a reboot, each answering a fresh message, with the stale row retired and one poller;
- a live release with a provably wedged event loop being terminated and replaced by A′, and a live release that neither hands over nor proves wedged being reported blocked with one poller;
- a pre-commit abort re-arming the poller, cron, kanban and autonomous wakeups, each proved by fresh work;
- SIGKILL of the old generation, the new generation and the updater.

A green hosted gate does not substitute for this suite, because hosted CI cannot run launchd.

### Authority And States

Use a per-install coordinator under the launch home, with per-profile keys for multiplexed sessions and token-scoped polling. The immutable `generation_id` is a UUID plus process PID, start fingerprint, pinned release SHA, launchd label and boot ID. A process cannot claim merely by naming a release. `active_generation` is a monotonically increasing epoch, not a bare PID or `current` symlink. An updater transaction lock serializes promotion, rollback, guardian repair and cap handling. SQLite uses WAL, `busy_timeout=5000`, foreign keys, `BEGIN IMMEDIATE` compare-and-swap transitions, and durable commit before any acceptance or polling offset acknowledgement. The updater owns `preparing → ready → transfer_requested → active` or `aborted`. Each generation process owns only the four runtime states of the forward-only amendment: `standby → serving → draining → exited`, with `standby → exited` for one that never serves. The old generation enters `draining` in the same transaction that commits the lease to its successor. Stopping new cron, kanban and autonomous wakeups on `transfer_requested` is a coordinator event inside `serving`, not a separate runtime state. A watchdog can set the `failed` verdict only after proving process death by PID **and** start fingerprint, or a witnessed clean exit. Heartbeat expiry alone is a health observation (`suspect`), recorded outside `state`, which stays unchanged. A suspect generation blocks transfer and is not dead or free to steal.

Suggested minimal schema (additive and versioned; timestamps UTC):

```sql
CREATE TABLE generations (
  id TEXT PRIMARY KEY, release_sha TEXT NOT NULL, label TEXT NOT NULL,
  pid INTEGER, start_fingerprint TEXT, started_at REAL NOT NULL, boot_id TEXT NOT NULL,
  scope_nonce TEXT,
  state TEXT NOT NULL, heartbeat_at REAL NOT NULL, drain_deadline REAL,
  verdict TEXT, verdict_at REAL, verdict_evidence TEXT
);
-- One live reservation per label; exited rows stay as history.
CREATE UNIQUE INDEX generations_live_label ON generations(label) WHERE state <> 'exited';
CREATE TABLE leases (
  resource TEXT PRIMARY KEY, epoch INTEGER NOT NULL, generation_id TEXT NOT NULL,
  state TEXT NOT NULL, FOREIGN KEY(generation_id) REFERENCES generations(id)
);
CREATE TABLE sessions (
  profile_home TEXT NOT NULL, transport TEXT NOT NULL, session_key TEXT NOT NULL,
  generation_id TEXT NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL,
  last_seq INTEGER NOT NULL DEFAULT 0, outstanding_work INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(profile_home,transport,session_key)
);
CREATE TABLE inbox (
  id INTEGER PRIMARY KEY, profile_home TEXT NOT NULL, transport TEXT NOT NULL,
  session_key TEXT NOT NULL, source_event_id TEXT NOT NULL, kind TEXT NOT NULL,
  seq INTEGER NOT NULL, owner_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL,
  authorized_source BLOB NOT NULL, payload BLOB NOT NULL, state TEXT NOT NULL,
  UNIQUE(profile_home,transport,source_event_id,kind),
  UNIQUE(profile_home,transport,session_key,seq)
);
CREATE TABLE telegram_updates (
  token_hash TEXT NOT NULL, update_id INTEGER NOT NULL, raw_update BLOB NOT NULL,
  state TEXT NOT NULL, PRIMARY KEY(token_hash,update_id)
);
CREATE TABLE polling_cursors (
  token_hash TEXT PRIMARY KEY, confirmed_offset INTEGER NOT NULL,
  generation_id TEXT NOT NULL, epoch INTEGER NOT NULL
);
```

`authorized_source` retains canonical `RoutingIdentity`, receiving bot, sender/chat/topic, profile and authorization result, not a freshly invented source on the other process. Store secrets neither in identifiers nor logs. Envelope schema versions and bounded payload sizes are mandatory. Idle sessions with no pending input, approval, turn, delegated child, watcher, goal or loop wakeup need no A ownership: B can claim on first subsequent event. Active session ownership remains A across a turn boundary until *all* its dependent async work resolves, explicitly detaches to durable owner-independent handling, or is interrupted once. The transfer transaction changes owner and moves queued rows together, preserving `seq`; no process rewrites an active agent history or prompt cache.

### Ordered Promotion And Polling

1. Updater stages B under immutable `releases/<sha>/`, checks both releases against a consistent DB snapshot and enabled plugin imports, writes `previous` and the S2 transaction receipt, and bootstraps the inactive launchd label from the **pinned absolute release path**. B starts in standby: generation-specific PID, status, control socket and logs, no Telegram `getUpdates`, cron tick, kanban claim or goal/loop wakeup. Its control endpoint reports readiness and the same profile/token roster as A. `current` may point to B for future launches, but A's interpreter/cwd/PYTHONPATH never resolve through it again. B must not use the ordinary duplicate-host attach, singleton PID claim, socket-unlink or token-lock takeover paths in standby.
2. Updater records `transfer_requested` under the transaction lock. A stops initiating `getUpdates` and disarms its reconnect watcher for each token; it does **not** disconnect the Bot API send/edit client. A persists received raw updates and the confirmed cursor, commits its last update dispatches, waits for the in-flight long poll to stop, releases `gateway.status.acquire_scoped_lock`'s token lock as the owning PID and acknowledges `poller_stopped(token,epoch,cursor)`. A stops new cron/kanban dispatch and internal autonomous wakeups but retains existing work. Never use `--replace`, which can kill A or take its token. If A cannot prove no outstanding poll, fail promotion closed; do not start B's poller on a timeout.
3. Updater atomically changes the admission/dispatch lease to B (increment epoch) only after A's stop receipt and B's readiness. B acquires the same scoped token lock (and must verify the expected old holder is gone), then starts one real `getUpdates` loop with `drop_pending_updates=False` from the last **durably confirmed** offset. The new gateway can now answer new sessions without waiting for A to drain. There can be a short no-poller transfer interval, but never two pollers; updates remain at Telegram or in the raw-update journal. A strict requirement for *one continuously running* getUpdates loop at every instant is incompatible with handing one process's loop to another without a separate permanent poller: this design instead promises at most one, a bounded zero-poller handoff, and lossless buffered updates. The updater verifies real poll progress and a synthetic authorized admission/output before acknowledging the release. A becomes `draining` and still sends/edits its own S4.1 outbox through its Bot API client. Both egress writers check the session owner/epoch for turn output; neither runs the other's outbox rows.
4. Telegram offset correctness is a separate acceptance gate. Current PTB advances its offset before the adapter's drop/accept guard. A durable dedupe receipt written only after `process_update` is insufficient. Interpose at the `getUpdates` batch boundary: persist every raw update *before* PTB can request `offset > update_id`; advance the shared confirmed cursor only after that journal commit, and replay journaled not-yet-admitted updates through the same authentication and idempotent admission path. B must seed PTB's offset from this cursor before first poll, not its default cold-boot behavior. If PTB cannot enforce this ordering, do not enable overlap. The `(token_hash, update_id, kind)` admission key and `inbox` uniqueness suppress redelivery across A and B. Committing ingress is not a claim that a tool ran or a Telegram reply was delivered.
5. A exits **0** only when its turns, delegations, approvals, subprocess watchers, outputs and owed goal/loop obligations are settled and its remaining session claims are transferred. The two-hour hard cap begins when A acknowledges poller stop. At the cap, fence A's session claims, mark unfinished operations interrupted with their original owner and side-effect evidence, preserve unsent/ambiguous S4.1 rows, then use the existing explicit recovery path. Never automatically re-run an interrupted tool or delegation. Bootout the now-idle A label only after exit/claim readback; retain its release while any process or receipt refers to it.

The transfer driver distinguishes a failed pre-commit handover from a committed lease whose successor has not proved polling progress. Before commit it aborts the transfer and asks A to re-arm polling and dispatch. After A acknowledges poller stop, A also keeps its own 45-second pre-commit deadline: if the driver disappears, A compare-and-swap aborts the exact transfer attempt and re-arms only while it still owns the lease. A failed re-arm keeps dispatch fenced and is visible as a stopped poller; a committed B always wins the lease race. After commit the driver raises `HandoverCommittedUnverified(generation_id, epoch)`, which is **not** a retry signal: the updater must inspect the committed lease and successor health before any rollback. The runtime-status writer uses `gateway_runtime.<generation>.json`; the heartbeat alone writes `gateway_state.<generation>.json` with socket and identity, while the lease holder projects the legacy summary and PID under its SQLite fence. During an overlap `gateway.pid` points to active B, so `hermes gateway stop` and `--replace` target B first, **not** the still-draining A; inspect `hermes gateway status` for A's PID before any intervention.

### Input And Completion Routing

B canonicalizes and authorizes each incoming event through the receiving adapter before looking up `(profile_home, transport, session_key)` in the coordinator. In one transaction it chooses a current owner and allocates `seq`, commits the original platform event ID and immutable source envelope, then returns only a **durable enqueue** receipt. A consumes rows addressed to its generation in order, checks ownership and source authorization again, and calls its own native adapter and runner path so both `platforms/base.py` and `run_busy.py` guards execute. A records `accepted` or explicit refusal; B must not treat its own enqueue as A's acceptance. Duplicate platform updates return the original row/disposition, never invoke the runner again. B exposes pending/unavailable rather than lying about a lost approval. If A dies between enqueue and acceptance, rows remain inspectable; dispatch to B only after death proof and an explicit never-executed disposition, otherwise interrupt/hold.

Route `/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny`, plaintext or inline approval, clarify text and mid-turn steer through this same sequencer. Control and approval events bypass both busy-message queues but are still ordered against the turn; only A's pending-approval registry may consume its answers, matched by pending approval ID, turn ID and sender. Duplicate or late answers are no-ops with a durable disposition. Ordinary follow-ups keep FIFO and A's native queued-turn behavior. Existing background-process completion watchers and native async-delegation completion callbacks remain running in A and inject into A-owned sessions; an old completion is not synthesized or reissued by B. Goal continuations and loop wakeups already owed to A stay A-owned; new goals/loops start on B. Any independently detached durable worker keeps its existing S1–S3 ledger owner and delivery rules. Cron jobs launched by A finish under their original pinned worker/session, while *only* B schedules new ticks for every served profile. The existing per-home `.tick.lock` and `scheduled_instant` deduplication stay defense in depth, not the sole dispatch fence. Kanban dispatch follows the same B-only epoch. Every callback captures its original profile/transport identity.

### launchd, CLI And Recovery

Give each generation its own label, `ai.hermes.gateway.g-<uuid>` (see the forward-only amendment), in one resolved `gui/<uid>` or `user/<uid>` domain, with `RunAtLoad` and `KeepAlive={SuccessfulExit=false}`. Plists pin interpreter, cwd and import path to an immutable release, not `current`. The first migration from today's `ai.hermes.gateway` needs a compatibility bridge in an already-running A: install the generation-aware A binary before enabling the flag, register A's live label and session claims, then bootstrap B under its own generation label; never bootout the legacy label while it owns work. The legacy label is the only one that is not generation-named, and it exits for good after its first drain. Do not replace or `bootout` A during the overlap: on this Mac a disposable `launchctl bootout` killed the supervised Python process **and** its `sleep` child. A separate disposable job with `KeepAlive.SuccessfulExit=false` exited 0 once and stayed loaded but not running. See the probe receipts in the design notes. A crash exits nonzero and may be respawned by launchd; the first gate of any respawn is the coordinator, which refuses a retired generation and cannot silently reclaim token/old sessions. Before the cap, an A crash does not preserve its in-process work; only an intact A process yields uninterrupted continuation.

`hermes_cli/update_cmd.py` and S2's `immutable_releases.py` must stage/commit the pointer and launchd layout as one recoverable transaction; `gateway_launchd.py` bootstraps the new generation's own label instead of reloading the active one. Fleet enumeration, update verification and `hermes gateway status` must report **both** generation identities, their SHA, label, PID/start fingerprint, active lease, polling owner and draining count. A single `gateway_state.json` is currently written by a process-local writer and would be clobbered: write `gateway_state.<generation>.json` per process and a fenced active-generation summary at the legacy path (or compute that summary from the coordinator). Likewise generation-scope `gateway.pid`, `gateway.sock` and host records; the stable control client resolves the active generation from the coordinator, while the updater can address A and B independently. Cleanup may remove only files whose generation and fingerprint match. Unflagged installs retain their current paths and behavior.

G1's guardian currently checks one label and one `gateway_state.json`, waits an acknowledgement grace defaulting to 180 seconds, and reboots/rolls back a failed switch by booting out a target. Under overlap it must resolve every live generation label and the active lease; never bootout a live drainer, never bootstrap a retired label to reclaim the token, and never mark an intentional clean drain as an outage. It watches the **60-second overlap health deadline**, distinct from S2's normal acknowledgement grace, and its only rollback action is to start a fresh generation on `previous` that takes the lease by handover or takeover. `current`/`previous` must be reconciled with the coordinator after updater death; the pointer is not admission authority. Loaded-but-unhealthy and unknown process identity fail closed with an alert, not automatic force takeover. Retention protects both live release paths and pending receipts.

Crash outcomes by boundary:

- Before B is ready: A retains polling/dispatch; a dead updater leaves a staged release only. After B is ready but before A's poller-stop receipt: A retains authority; B remains standby. Acknowledged poller stop but before B starts: guardian or recovering updater first checks A's exact process and token lock, then either completes the B transfer or re-enables polling on healthy A within 60 seconds. Never assume rollback from a pointer alone.
- After B claims the token: a dead updater is observer-only; B remains active and its receipt is recoverable. If B fails health within 60 seconds, start a fresh A′ on `previous`. A′ takes the lease by handover if B is live, after B stops its poll and fences new admissions, or by takeover once B is proven dead. `previous → current` changes only after A′ acknowledges from its release. The draining A is never re-armed. B's already accepted turns remain B-owned to completion if B is alive; otherwise mark interrupted after death proof. If neither B's poll stop nor its death can be proven, report blocked/unknown, never allow simultaneous polls. Successful rollback readback includes label, lock holder, release SHA, cursor and a fresh message answered by A′.
- SIGKILL of A during drain: existing in-process turn, approval, delegation and tool work is interrupted, not transferred live. Preserve journal/inbox/outbox rows, prove owner dead via fingerprint and supervisor state, mark once, never replay side effects. B continues new work. SIGKILL of B: stop/absence of B polling must be proved before any generation takes the token. A fresh A′ on `previous` takes over within 60 seconds, while B-owned work is interrupted/held. The draining A keeps draining either way and never takes the token back. Recover only safe durable input, not active turns. A suspended/wedged process with stale heartbeat is not dead and cannot be fenced out of external side effects by SQLite alone.
- On two-hour expiry, stop admitting into A, persist interrupted dispositions and release references, signal graceful stop, then terminate its **verified** label only after its claim and outbox readback. A tool subprocess that escaped launchd's coalition needs the existing process-registry owned-tree termination policy. A hard kill cannot make the user's already-running external side effect atomic.

### Compatibility, Slices And Acceptance

The shared session/cron/outbox databases and shared `~/.hermes/plugins` must work concurrently in releases A and B. Every coordinator schema change is additive; both sides negotiate a fixed envelope version during staging, and incompatible major versions block promotion. Run `scripts/schema_rehearsal.py` against a consistent copy in both release interpreters, and a read/write/up/down compatibility rehearsal of enabled plugins and each shared DB. Never run a destructive migration during overlap or rollback. A stale reader writing an old whole-file snapshot must be identified and either made concurrency-safe or promotion blocked. A and B keep distinct in-memory caches; only idle session claims change owners and rebuild on B at the next turn, without retroactive prompt edits.

Each slice is a separate PR, behind `gateway.forward_only_handover.enabled: false` by default, with the old drain-first route unchanged when off. `gateway.overlap_handover.enabled` is the pre-amendment key and stays false (see Flag and previous release):

1. **Generation isolation:** two disposable launchd labels, pinned paths, noncolliding PID/control/status/host records and accurate fleet/guardian/status readback. Proof: live A and standby B coexist without affecting the normal gateway or one another.
2. **Lossless polling transfer:** raw-update journal, cursor/offset interposition, token lock and no-poller standby; real stub Bot API proves one poller and no lost or double-admitted update across every kill boundary. If PTB ordering cannot be controlled, stop here.
3. **Owned admission:** WAL leases/inbox and native two-guard routing for follow-up, stop, approval and clarify, with fenced outbox sender. Proof: two release-pinned gateways, a 60-second tool turn on A while B answers a new session and A's follow-up/approval is consumed once.
4. **Async drain and supervision:** delegation, process watchers, goals, loops, cron, kanban, cap and failover integrated with updater, guardian and S2 receipts. Proof: A's native delegation and approval finish, one cron occurrence, B handles new work, A exits 0 after last obligation, and a failed B health probe hands the lease to a fresh A′ on `previous`, which answers a fresh message within 60 seconds. Run the actual disposable-profile `launchctl` rehearsal, not only mocks; assert both release SHAs, user-visible outbox receipts, both guards and DB/plugin compatibility. Only the top-level owner can authorize a live switch.

The earlier S4.2 STOP items are answered narrowly. Distinct pinned A/B releases and native long tool are *not yet proved* (#1); the original process keeps native delegation and approval registries rather than extracting them (#2–3); native base and runner busy guards stay together on A, with only ordered raw-event forwarding (#4); streaming edits and direct Bot API sends remain in A, so no router-replacement streaming RPC is needed (#5); the actual scoped token lock and real getUpdates transfer still require the second slice (#6); S4.1 already held 20 ambiguous sends without duplicates in a component spike, but the whole-system crash/receipt behavior still requires E2E proof (#7). The proposal avoids the router/executor split; it does **not** avoid the hard Telegram ingress-offset, authorization, shared-state and crash-ambiguity problems. Stop at the first failed native acceptance case rather than interpreting a green component test as rollout authority.

## Delivery gates and open decisions

Implement **one PR per slice** against the fork's current `main`, with independently runnable acceptance evidence and no forced gateway update as part of source landing. Each PR includes focused native-host tests and the relevant repository-owned `./bin/ci gate <sha>`; run the full portable gate where shared contracts change. Record exact source/release SHAs, observed process identities, ledger state, pointer target, ownership lease, health result, and rollback result in tests/receipts. Schema-affecting slices require `scripts/schema_rehearsal.py` on a consistent copy before activation. The parent owns integration and separate runtime-activation authorization.

Before S1 code, decide and test the macOS descendant-termination authority when a child double-forks away from the launched session; detachment alone does not solve hard kill. Before S2 activation, establish whether a venv is genuinely reusable across both exact dependency sets and how live plugin changes remain dual-version-compatible. Before S4.1, prove the durable admission/outbox contract including the external send/receipt ambiguity. Before overlap activation, demonstrate both busy guards and approval/steer/stop dispatch through the native owning gateway, with one Telegram poller and durable ingress offset. Prove generation fencing, dual-version compatibility and rollback on actual launchd processes. If those decisions cannot be demonstrated, stop at the affected slice rather than weakening its acceptance criteria.

### S2 settled decisions (implementation contract)

- **Virtual environments are per release and built in place.** Each `releases/<sha>/` owns `.venv`, built at its final path from that release's lockfile (`uv sync --frozen`). Do not physically clone or relocate a venv: shebangs, activation scripts, and metadata can retain the old absolute path. A warm-cache build on this Mac in a disposable source export took 1.60s (`/usr/bin/time -p mise x uv@0.12.13 -- uv sync --frozen --python <base-interpreter>`); this is a measurement, not a service-activation result.
- **Plugins remain shared.** `HERMES_HOME/plugins` is owned and changed independently by agentkit. Promotion does not modify it. Staging copies the relevant plugin tree and config into a disposable profile and imports enabled plugins with the candidate interpreter (without calling `register()`); an import failure aborts before the `current` pointer changes. Agentkit must keep plugin releases compatible with both current and previous Hermes releases; that compatibility contract is outside S2.
- **Promotion order is transactional.** Stage the fetched source, private venv, and plugin smoke; write `previous` to the old target; atomically replace `current`; then retain `current`, `previous`, three rollback-capable releases, and every release pinned by a live process or receipt. A failed stage leaves the old pointer untouched.
- **Runtime pinning.** launchd definitions resolve program, cwd, venv, and import path through `current`; a detached cron worker resolves `current` once at launch and pins the resulting release path for its executable, cwd, and `PYTHONPATH`. `~/.hermes/hermes-agent` remains the git source checkout and is not replaced by a release.
- **Migration and rollback.** The first promotion creates the current-HEAD release and updates the existing launchd plist through `hermes_cli/gateway_launchd.py`; `hermes update --rollback` atomically points `current` at `previous` and uses the existing restart/report/receipt path. A migration rollback points the plist back to the source checkout. Do not activate this migration on the live install without the parent owner's separate authorization.

### G1 Guardian (Opt-In, macOS launchd)

`gateway.guardian.enabled: true` enables the independent one-shot launchd guardian; it is off by default and is not installed or loaded merely by setting the key. On an immutable release with an installed gateway plist, `hermes gateway guardian install` installs its separate launchd job; `hermes gateway guardian status` reports the enable flag, installation and stopped intent; `hermes gateway guardian uninstall` removes the guardian job. Run these only for the intended profile, not as a consequence of landing source code.

A deliberate gateway stop writes `<HERMES_HOME>/gateway-guardian-stopped`; a start clears it before dispatch so failed start attempts do not leave false stopped intent. While the marker exists the guardian does not repair an unloaded gateway. It waits through `updates.release_acknowledgement_timeout_seconds` (default 180 seconds) before judging an unacknowledged release switch, and rejects stale runtime status. With an intact `current` release and matching launchd plist, it can bootstrap an unloaded gateway or roll back a failed switch to a verified `previous` release. It never repairs a corrupt pointer from the source checkout. A nonblocking lock and a cap of three bootstrap/rollback attempts per hour prevent a repair loop; loaded but unhealthy services are left to launchd or operator inspection rather than force-repaired.

Inspect `<HERMES_HOME>/logs/guardian/` for JSON attempt, result, capped and alert receipts, plus `stdout.log` and `stderr.log`. Identical recent alerts are deduplicated and receipts older than an hour are pruned. The guardian does not manage router/executor handoff or retry interrupted work.

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
  - Removing this cost requires the unimplemented [Overlap Handover](#overlap-handover), with native acceptance evidence before activation.
- **Send outcome is uncertain after a crash.** The outbox holds any send whose outcome is unknown and never resends it. A crash between the platform accepting a message and the receipt commit can therefore leave a reply unconfirmed in the outbox while the user did receive it. A refused connection is treated as definitely unsent. Missing platform ids get random ids, and a multi-part send shares one record.
  - An adapter refusal made before any request (Telegram `Not connected` or `send_path_degraded`) carries `SendResult.pre_send` and records `failed_unsent`, so the same payload may be dispatched again. A final refused this way is handed to the delivery ledger, whose reconnect sweep redelivers it.
  - An outbox hold carries `SendResult.held`. `_send_with_retry` returns it as final: it never retries and never sends the "Response formatting failed" plain-text copy, which would get past the duplicate guard as a new payload.
- **The guardian never force-repairs.** It leaves a loaded-but-unhealthy gateway alone, stops after three repairs per hour, and treats a wedged heartbeat older than 120 s as unhealthy. It never repairs a corrupt `current` pointer from the source checkout.
- **Some hardening paths have only test evidence.** The manual-run kill, double-fork sweep and commit/enqueue-gap paths have regression tests but no live trigger yet.
- **The full unscoped update path can only be proven live.** Disposable rehearsals cover it with throwaway launchd labels.

## Overlap Design Decision

The earlier S4.2 router/executor split remains stopped: draft PRs [#215](https://github.com/0xble/hermes-agent/pull/215) and [#224](https://github.com/0xble/hermes-agent/pull/224) did not establish the seven native end-to-end proofs. Brian's decision to stop that split is unchanged. The [Overlap Handover](#overlap-handover) instead designs two native gateways with no streaming router/executor IPC; it has not passed a native prototype, and S1–S3/S4.1/G1 remain the shipped fallback. On a failed poller/offset, shared-state or rollback acceptance test, retain the drain-first route rather than enabling a partial handover.

On 2026-09-30 the in-place rollback path failed native acceptance, as described in the [Forward-Only Amendment](#forward-only-amendment-2026-09-30). Rollback now starts a fresh generation on the previous release instead of re-arming a draining one. Handover code must follow the amendment. Code that restores a draining generation does not merge.
