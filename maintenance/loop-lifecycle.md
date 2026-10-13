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

## Proof surface

Focused tests cover LoopState compatibility and versioning, authority branches, cadence re-anchoring, preservation of ticks/route/created_at, replacement, status and wakeup text, receipt registry behavior, and stale-manager survival through CLI/TUI completion paths. `tests/hermes_cli/test_loop_settlement.py` covers continue/done/blocked races and stale revisions with the real SessionDB. Run the affected tests through `scripts/run_tests.sh`; run the repository's canonical gate before publication.

## Upstream disposition and retirement

This is a fork-only core adaptation for the external `loop_set` plugin contract. An upstream PR is a follow-up once the plugin contract and native `/loop` lifecycle API have stabilized. Retire this unit only when released upstream behavior provides the same versioned revisions, authority split, cross-surface cache freshness, and receipt registry contract; remove the fork implementation and tests after equivalent upstream coverage is verified.
