# Loop lifecycle

Identity: `loop-lifecycle`

## Required behavior

Keep `/loop` agent-editable without allowing an agent to silently make a loop more active than the user's instruction permits. `LoopManager.revise` records versioned changes, requires a verbatim user quote for prompt, stop-condition, faster-cadence, or increased-run authority, and preserves in-flight lifecycle state. `replace` is the explicit full-definition path and always requires user authority.

The CLI's cached manager must refresh before due checks and post-turn completion so a `loop_set` revision made during a wakeup cannot be overwritten by stale completion state. Gateway and TUI paths construct fresh managers and must retain that cross-surface behavior. Receipts for `goal_set` and `loop_set` use their configured `auto_notices` gates and distinct notice keys, while requiring successful persistence readback.

## Proof surface

Focused tests cover LoopState compatibility and versioning, authority branches, cadence re-anchoring, preservation of ticks/route/created_at, replacement, status and wakeup text, receipt registry behavior, and stale-manager survival through CLI/TUI completion paths. Run the affected tests through `scripts/run_tests.sh`; run the repository's canonical gate before publication.

## Upstream disposition and retirement

This is a fork-only core adaptation for the external `loop_set` plugin contract. An upstream PR is a follow-up once the plugin contract and native `/loop` lifecycle API have stabilized. Retire this unit only when released upstream behavior provides the same versioned revisions, authority split, cross-surface cache freshness, and receipt registry contract; remove the fork implementation and tests after equivalent upstream coverage is verified.
