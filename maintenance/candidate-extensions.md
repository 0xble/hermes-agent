# Candidate extensions and canonical skills

Load this unit when changing `candidate-extensions/`, `plugins/canonical-skill-guard/`,
the extension installer, or the personal skill curation procedure. The
[root contract](../MAINTENANCE.md) owns baseline selection and publication.

## Required behavior

- The four personal plugins (`goal-lifecycle`, `memory-journal`, `request-update`,
  `review-candidate`) install through real plugin discovery and register their tools
  (`goal_set`, `memory_undo`, `memory_journal_list`, `request_update`, `review_candidate`).
  Review completion binds to the dispatched child; request-update uses native spawn/watch.
  Committed `goal_set` mutations carry a `notice` receipt that
  `agent/inline_tool_executors.py` surfaces through the agent notice lane
  (`goals.auto_notices`).
  New goals inherit the active profile's `goals.max_turns`, with the native
  default for absent or invalid values. Model arguments cannot override that
  policy. Reading or extending an existing goal preserves its stored budget.
- The memory journal is hash-chained, verifiable, and growth-bounded.
- `plugins/canonical-skill-guard/` enforces personal skill ownership on canonical external
  skill writes. `scripts/curate_skill_observations.py` must verify PR success before treating
  observations as processed.
- Neither company release ships these extensions; do not infer plugin acceptance from a
  runtime-source upgrade.

## Provenance and patches

- Fork patch identities: `slice-3-goal-lifecycle`, `slice-4-review-gate`,
  `slice-5-memory-journal`, `slice-6-skill-curation`, `slice-6-skill-guard`,
  `slice-7-hindsight`, `slice-14-request-update`, `candidate-tooling`.
- Upstream contribution: none recorded. Reassess the route when changing this behavior.
- Surfaces: `candidate-extensions/*`, `plugins/canonical-skill-guard/`,
  `scripts/install_candidate_extensions.py`, `scripts/curate_skill_observations.py`,
  `agent/inline_tool_executors.py` (notice lane).

## Verification

`scripts/run_tests.sh` on `candidate-extensions/*/test_*.py`,
`tests/plugins/test_candidate_extension_schemas.py`,
`tests/plugins/test_candidate_extensions_install.py`,
`tests/plugins/test_canonical_skill_guard.py`,
`tests/plugins/test_hindsight_root_guard.py`, and
`tests/agent/test_goal_set_receipt_notice.py`. `scripts/check_fork_patches.py` proves the
installed profile registers every extension tool through discovery.

## Retirement and rollback

Retire the goal, review, journal, and update-request patches when upstream ships an
agent-callable goal tool with a restricted action set, a `/review` that accepts a ref and
records a receipt, a memory ledger comparable to `skill_ledger.py`, and an agent-callable
update trigger, respectively. Retire the skill guard and curator when upstream adds an
external-dir-aware write guard and observation channel. Retire `candidate-tooling` when the
extensions ship as packaged plugins. Roll back by reverting the logical patch and removing
the installed plugin copies from the profile.
