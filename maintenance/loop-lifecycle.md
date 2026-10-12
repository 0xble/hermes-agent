# Loop lifecycle

Identity: `loop-lifecycle`

## Required behavior

Keep `/loop` agent-editable without allowing an agent to silently make a loop more active than the user's instruction permits. `LoopManager.revise` records versioned changes, requires a verbatim user quote for prompt, stop-condition, faster-cadence, or increased-run authority, and preserves in-flight lifecycle state. `replace` is the explicit full-definition path and always requires user authority; it keeps a paused loop paused because resume is user-only. Switching to self-paced counts as faster whenever the self-paced floor is below the current delay. `/loop wakeup` prompts are runtime text, never a source for a user quote. A wakeup with nothing new to report replies with a bare `[SILENT]`, which every surface hides and which never reaches the `--until` judge (see `internal-notification-silence`).

The CLI's cached manager must refresh before due checks and post-turn completion so a `loop_set` revision made during a wakeup cannot be overwritten by stale completion state. A failed refresh read keeps the cached state rather than dropping an in-flight tick. Gateway and TUI paths construct fresh managers and must retain that cross-surface behavior. Receipts for `goal_set` and `loop_set` use their configured `auto_notices` gates and distinct notice keys, while requiring successful persistence readback.

Revisions, replacements, and tick settlement stage changes against a durable snapshot and
commit only after a transactional compare-and-swap. Every persisted write carries a
mutation token, including same-value lifecycle changes. A stale revision returns
`state_changed` and refreshes its manager; it cannot overwrite a newer definition or
lifecycle control. A judge verdict for an obsolete snapshot is discarded without a
completion/blocker notice. If a newer revision retained the same in-flight tick,
release only that claim and schedule from the winning cadence with a second CAS;
never release a replacement/new tick or alter paused/cleared state. Existing quote,
cadence, run-count, and prompt authority policy is unchanged.

The initial comparison uses the manager's complete canonical state, not only its
mutation token, so unequal legacy rows with empty tokens still conflict. No-op
returns validate inside the same SessionDB write transaction. Each new tick also
carries a claim ID that revisions retain, so abandon/re-fire cannot impersonate
the old claim even when its timestamps and counters match. Already-running legacy
ticks use their creation/fire/count identity until the next claim. Operational
read/write failures log the cause and return `persistence_failed`, preserving
staged edits only in the discarded copy. Only a manager with an empty session ID
uses intentional ephemeral operation.

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
cadence/quote-authority policy. The moved revision bodies are unchanged except
for late imports and their CAS wrappers.
The gateway persists loop ownership in three phases: ``claimed`` before adapter admission, ``executing`` immediately before model work, and settled after the turn. On startup or a later scan, it may refund a positively unstarted claim only when no live, admitted, queued, or restart-recovering owner exists. Executing, legacy, or otherwise ambiguous claims are parked with an explicit unknown-effects reason; they are never blindly replayed. Explicit ``/new`` retires the old conversation's loop and goal instead of transferring them; compression follows the existing lineage tip and preserves responsibility. Topic identity remains bound to the active session; reset and compression are separate boundaries.
The gateway persists loop ownership in three phases: ``claimed`` before adapter admission, ``executing`` immediately before model work, and settled after the turn. Post-turn settlement requires the synthetic wake's exact `gateway_loop_tick` identity; unrelated human/internal turns, missing or malformed identities, and stale loop-instance/tick identities leave the claim untouched, even when their response contains `LOOP_COMPLETE`. On startup or a later scan, it may refund a positively unstarted claim only when no live, admitted, queued, or restart-recovering owner exists. Executing, legacy, or otherwise ambiguous claims are parked with an explicit unknown-effects reason; they are never blindly replayed. Explicit ``/new`` retires the old conversation's loop and goal instead of transferring them; compression follows the existing lineage tip and preserves responsibility. Topic identity remains bound to the active session; reset and compression are separate boundaries.

A due gateway loop defers before claiming or entering the adapter whenever another live, admitted, queued, or restart-recovering turn owns the conversation, and rechecks that ownership after the claim's executor hop: an owner that appeared there refunds only the still-unstarted claim. Independently, gateway-authored synthetic events (`internal`) never answer a pending clarify, `/update` prompt, or slash-confirm, at both the adapter's inline clarify bypass and the runner's reply intercepts; they take normal busy-session queuing, so wake text can never stand in for a human answer. A native /loop slash-command wake persists ``executing`` before any command handler runs, because quick-command exec and plugin handlers perform effects outside the model turn; the route/session control identity and exact executing tick are revalidated after that executor hop before dispatch. The loop state also carries a monotonic control fence, checked synchronously at execution entry, so no control can win the executor return gap and still allow stale model work. `save_loop` is the single fence writer: it reads the stored row in the same transaction and bumps the fence whenever a write changes status or loop instance, or (outside the tick's own claim/settle writes) clears or replaces the in-flight tick, so every control path (set, replace, pause, resume, clear, `clear_loop`, session reset) is covered without per-path bumps; a definition-only revise keeps the tick and the fence. A cached copy can never write an older fence back, and a tick claim is a compare-and-swap against the stored row's fence: a manager that loaded before another manager's control is rejected and refreshed instead of claiming. A parametrized native test enumerates the public `LoopManager` controls from the class, so a new control without fence coverage fails. Asynchronous command resolution is fenced again after command hooks and immediately before effectful canonical, quick, or plugin dispatch. An interruption inside them is parked as unknown-outcome, never refunded and replayed. A superseded tick skips the command. Command-only completion settles the exact tick at the handled dispatch boundary; prompt, quick-command alias, plugin, and skill rewrites fall through to the model-turn fence, which accepts the same already-executing tick, and settle after the model turn. Legacy direct adapters without native admission retain inline completion.

## Proof surface

Focused tests cover LoopState compatibility and versioning, authority branches, cadence re-anchoring, preservation of ticks/route/created_at, replacement, status and wakeup text, receipt registry behavior, and stale-manager survival through CLI/TUI completion paths. `tests/hermes_cli/test_loop_settlement.py` covers continue/done/blocked races, stale revisions, legacy entry checks, no-op and commit-edge conflicts, pause/resume ABA, claim cleanup races, persistence failures, and ephemeral operation with the real SessionDB. Run the affected tests through `scripts/run_tests.sh`; run the repository's canonical gate before publication.

## Upstream disposition and retirement

This is a fork-only core adaptation for the external `loop_set` plugin contract. An upstream PR is a follow-up once the plugin contract and native `/loop` lifecycle API have stabilized. Retire this unit only when released upstream behavior provides the same versioned revisions, authority split, cross-surface cache freshness, and receipt registry contract; remove the fork implementation and tests after equivalent upstream coverage is verified.
