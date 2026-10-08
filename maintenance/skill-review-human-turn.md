# Background skill review human-turn gate

Load this unit when changing the automatic background skill-review trigger or its
human/synthetic turn provenance gate.

- Fork patch identity: `skill-review-human-turn`.

## Behavior

Automatic background skill review keeps accumulating tool iterations but fires
only after at least one human-authored turn since the previous automatic skill
review (or session start). Synthetic goal continuations, process/delegation
notifications, and other Hermes-generated turns do not satisfy the gate. Memory
review behavior, explicit `/refine`, cron suppression, and curator behavior are
unchanged.

## Provenance

Fork-only at this revision. Upstream still checks only the iteration threshold
in `agent/turn_finalizer.py` and `agent/codex_runtime.py` (upstream main
`4f9b741a3a0284ecce31dc7e4cffde49173ec764`, fetched 2026-10-08).

Related upstream prior art:

- [#99661](https://github.com/NousResearch/hermes-agent/issues/99661) and
  [#99680](https://github.com/NousResearch/hermes-agent/pull/99680) address
  repeated pending skill writes by target deduplication, not synthetic-turn
  eligibility.
- [#57626](https://github.com/NousResearch/hermes-agent/issues/57626) and
  [#57646](https://github.com/NousResearch/hermes-agent/pull/57646) exclude
  sub-agent sessions from skill nudges, not runtime-generated turns in the
  foreground session.
- [#42391](https://github.com/NousResearch/hermes-agent/issues/42391) and
  [#42392](https://github.com/NousResearch/hermes-agent/pull/42392) defer review
  during active goal continuation, a separate scheduling race.
- [#127973](https://github.com/NousResearch/hermes-agent/issues/127973) documents
  structured `display_kind` provenance for model-only and automation rows; this
  patch reuses the existing provenance classifier rather than changing storage.

No exact upstream fix was found, and upstream main retains both sibling
iteration-only triggers.

## Verification

- `scripts/run_tests.sh tests/agent/test_skip_background_review.py -k 'skill_review_' -v --tb=short`
- `scripts/run_tests.sh tests/agent/test_skip_background_review.py`
- Full affected test modules and the portable gate are run for the PR candidate.

## Retirement and rollback

Retire when released upstream requires a human-authored turn for automatic skill
review across both finalization paths while preserving this complete behavior.
Rollback reverts this commit.
