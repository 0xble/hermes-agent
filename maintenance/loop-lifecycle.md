# Loop Lifecycle

Identity: `loop-lifecycle`

## Required Behavior

Keep `/loop` agent-editable without allowing an agent to silently make a loop more active than the user's instruction permits. `LoopManager.revise` records versioned changes, requires a verbatim user quote for prompt, stop-condition, faster-cadence, or increased-run authority, and preserves in-flight lifecycle state. `replace` is the explicit full-definition path and always requires user authority; it keeps a paused loop paused because resume is user-only. Switching to self-paced counts as faster whenever the self-paced floor is below the current delay. `/loop wakeup` prompts are runtime text, never a source for a user quote. A wakeup with nothing new to report replies with a bare `[SILENT]`, which every surface hides and which never reaches the `--until` judge (see `internal-notification-silence`).

Loop/goal overlap guidance uses the scheduler's `goal_blocks_loop_tick` predicate: only an active, non-waiting goal initially defers loop ticks. Parked/waiting, paused, and done goals do not block them, and the warning appears only when the goal currently blocks ticks. In the gateway, an idle active goal can defer a due tick for at most 1 loop interval (the floor for self-paced loops). Existing live-turn and continuation ownership fences still defer the tick at any age. Goal deferral and full-background-gate deferral each log the session and reason at INFO, at most once per session per loop interval. The background gate remains mandatory and leaves refused ticks unclaimed and due.

The CLI's cached manager must refresh before due checks and post-turn completion so a `loop_set` revision made during a wakeup cannot be overwritten by stale completion state. A failed refresh read keeps the cached state rather than dropping an in-flight tick. Gateway and TUI paths construct fresh managers and must retain that cross-surface behavior. Receipts for `goal_set` and `loop_set` use their configured `auto_notices` gates and distinct notice keys, while requiring successful persistence readback.

Every loop write goes through `save_loop`'s stored-row transaction, and every manager
publishes the result through the `commit_and_publish_snapshot` boundary it shares with
goals. In that transaction `save_loop` assigns both writer-owned tokens: a fresh mutation
token on every durable write, including same-value lifecycle changes, and the control
fence. The fence advances when a write changes status or loop instance, or clears or
replaces an in-flight tick outside tick settlement. A definition-only revise preserves
the tick and fence. Publication adopts the committed row only while no newer
same-manager command has published, and otherwise keeps or reloads the durable winner,
so a pause, clear or replacement that lands after a commit is never overwritten in
memory. Short lifecycle controls serialize with publication, not with model judging.

User controls (set, pause, resume, clear, `clear_loop`, reset) apply only their
command-owned edits to the current stored row, so a stale pause or resume cannot revive
a cleared row. Tick claim, release, skip and recovery also apply to the stored row,
behind one outcome fence: still this in-flight tick, in the expected phase, with no live
owner. Agent revisions, replacements and tick settlement stage on a copy instead and
commit only while the stored row still equals the exact snapshot they read. A stale
revision returns `state_changed` and refreshes its manager. It cannot overwrite a newer
definition, lifecycle control or tick. Legacy cached saves compare the full synchronized
snapshot and reject newer rows.

A judge verdict for an obsolete snapshot is discarded without a completion/blocker
notice. If a newer revision retained the same in-flight tick, release only that claim
and schedule from the winning cadence with a second CAS. Never release a replacement or
new tick, or alter paused/cleared state. A settlement that loses to compression retries
against the validated child only while it still holds the same claim. Requested
run-count and configured budget caps govern every settlement, release and skip. Existing quote, cadence,
run-count, and prompt authority policy is unchanged.

The initial comparison uses the manager's complete canonical state, not only its
mutation token, so unequal legacy rows with empty tokens still conflict. No-op
returns validate inside the same SessionDB write transaction. Each new tick also
carries a claim ID that revisions retain, so abandon/re-fire cannot impersonate
the old claim even when its timestamps and counters match. Already-running legacy
ticks use their creation/fire/count identity until the next claim. Operational
read/write failures log the cause and return `persistence_failed`, preserving
staged edits only in the discarded copy. A cursor owner runs the snapshot read and CAS
through its own transaction and sees write failures directly. Only a manager with an
empty session ID uses intentional ephemeral operation.

Conflict cleanup compares against the isolated initial snapshot, never the live
manager reference. Commands on that same manager during judging may pause,
resume, abandon, or fire another tick. They cannot mutate the cleanup identity
and cause the old response to release a newer claim.

Successful empty turns, failed display notices, and interrupted turns remain
separate admission outcomes. The goal admission decision does not prevent loop
claim cleanup. Failed/incomplete output is not judged as loop completion evidence,
and the CLI's existing Ctrl+C pause remains authoritative. Gateway evaluation
uses the agent's raw final output, including explicit emptiness, before display
normalization can manufacture retry/cleanup notices. Silent replies still
skip the loop judge. This repair changes neither self-paced backoff nor the user's
cadence/quote-authority policy.

The gateway admits a wake with 1 stored-row compare-and-swap that both claims the tick and marks it `started`. The transaction checks the stored loop instance, active/idle state, tick count, control generation and schedule, and records the wake's run-generation token. Loop discovery binds only the existing home context, so admission runs before turn preparation, profile hydration, `agent:start`, command hooks, command dispatch or model work. A failed admission CAS starts no work. An admitted tick is never refundable, even if adapter admission fails or a producer is cancelled after an effect.

On startup or a later scan, recovery first proves that no live, admitted, queued or restart-recovering turn owns the tick, and repeats that check at the stored-row settlement boundary. It consumes an interrupted tick as skipped with an unknown outcome and schedules the next tick on the stored cadence. It never replays the interrupted tick or pauses the loop awaiting inspection. Legacy `claimed`, `executing` and unmarked rows also have unknown outcomes and follow the same recovery rule. Post-turn settlement requires the synthetic wake's exact `gateway_loop_tick` identity. Unrelated human/internal turns, missing or malformed identities, and stale loop-instance/tick identities leave the claim untouched, even when their response contains `LOOP_COMPLETE`.

A due wake defers before claiming whenever another live, admitted, queued or restart-recovering turn owns the conversation. The admission CAS repeats the local ownership check. An owner appearing after successful admission leaves a consumed tick for recovery, never a refunded tick or wake text offered as a human reply. Gateway-authored synthetic events (`internal`) never answer pending clarify, `/update` or slash-confirm prompts, including the adapter's inline clarify bypass. Explicit `/new` retires the old conversation's loop and goal. Compression follows the existing lineage tip and preserves responsibility. `/resume` and topic rebinds are reversible, so a loop whose route resolves to another, non-tip session defers untouched until the route returns; the scanner never retires a loop. A restart marker counts as an owner only inside the auto-resume freshness window. The loop and goal scanners share one stale-marker rule: a stale marker is compare-and-swap cleared before the scan acts, and a refreshed marker wins.

## Proof Surface

Focused tests cover LoopState compatibility and versioning, authority branches, cadence re-anchoring, preservation of ticks/route/created_at, replacement, status and wakeup text, receipt registry behavior, and stale-manager survival through CLI/TUI completion paths. `tests/hermes_cli/test_loop_settlement.py` covers continue/done/blocked races, stale revisions, legacy entry checks, no-op and commit-edge conflicts, pause/resume ABA, claim cleanup races, same-manager commands after a revision, settlement or claim commit, persistence failures, and ephemeral operation with the real SessionDB. `tests/hermes_cli/test_loop_control_atomic.py` covers stale user controls and revisions against every newer lifecycle, write failures, and cursor-owned claims, settlements and revisions. Run the affected tests through `scripts/run_tests.sh`; run the repository's canonical gate before publication.

### Execution-Entry Fence

`gateway/run_loop_wake.py::GatewayLoopWakeMixin` owns the shared execution fences inherited by turn, inbound and goal hosts; non-loop events pass through without runner state.

The exact session, tick, control fence and owned run-generation token travel on the event's `_gateway_loop_execution_fence`. Preparation and command/model paths revalidate that identity after awaits. Async effect producers read the stored row through the existing context-preserving executor because SessionDB opens and locks are forbidden on the gateway loop thread. Synchronous plugins read it in the worker that invokes the handler. A synchronous local control/run check immediately precedes invocation, with no await in the final check-to-call interval. Controls that win the executor-return gap therefore suppress stale work, while an interruption always leaves a started tick for skip-only recovery.

The process-local fence is keyed by store home and session, and a retired loop leaves none, so the synchronous check fails closed for it.

Sentinel installation performs the same synchronous check. All running-agent, lease and override cleanup uses the wake's captured token, never the current generation after an await. A stale wake cannot overwrite or release a successor agent or its lease. `/learn`, `/plan`, hook rewrites, aliases, plugins and skills that fall through to the model remain in the same wake and generation. No handoff record, phase promotion or generation rebind exists. Hook rewrites to `/plan` or `/learn` still enter the canonical handler to build the same constrained prompt as a direct invocation. Synthetic prompt builds suppress transport acknowledgments. Command-only completion settles the exact tick only when dispatch actually ran; fence-suppressed hooks, handlers, exec commands and plugins keep the tick started for skip-only recovery without a finished notice. Model fallthrough settles after the model turn. Legacy direct adapters without native admission retain inline completion.

Pre-turn hygiene compression can move the started tick to a child during preparation. The fence follows only the validated compression successor that holds the same started tick, run generation and control fence, and rebinds the wake to it. Any other route defers without settling. Every fence-suppressed exit logs the session, tick and reason.

A watcher whose parent route resolves to the matching compression tip re-homes the loop with the existing migration owner: one stored-row CAS preserves the claim and retires the parent under the control fence. A failed migration logs and retains the parent for retry, never orphan-clears it.

Native behavior tests cover cancellation after admission, during preparation, after an `agent:start` effect and at model entry. Recovery consumes tick 1 and runs tick 2 without repeating tick 1. They also cover public `/new` at the final `/learn` command-check boundary with a generation-2 successor agent and lease, canonical hook-rewritten prompts, strict zero/nonzero tokens, every loop control and separate-process controls that cannot update the local cache.

## Upstream Disposition and Retirement

This is a fork-only core adaptation for the external `loop_set` plugin contract. An upstream PR is a follow-up once the plugin contract and native `/loop` lifecycle API have stabilized. Retire this unit only when released upstream behavior provides the same versioned revisions, authority split, cross-surface cache freshness, and receipt registry contract; remove the fork implementation and tests after equivalent upstream coverage is verified.
