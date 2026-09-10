# Canonical Telegram delegation presentation

## Contract and evidence

The reported persisted topic had two unretired presentation keys. One source used
`profile: null` and the other `profile: default`, while both execution owners were
`default`. `_scope` treated these as different conversations. `_bind` then returned
early for any valid existing key, so restart reconciliation could not repair the
split. The earlier retired-anchor fix did not address either cause. This is a
private-fork presentation defect, not evidence of duplicate executions; upstream
prior-art lookup is not applicable to this local overlay.

The correction normalizes an absent routing profile to its owning profile and
selects one presentation under the same scope lock used by lifecycle events and
transport. Immutable task records, actual delegation owners, ancestry, authored
refs, handled receipts and outcomes remain separate. An exact cleanup ledger is
persisted before transport; a successful aggregate edit/send is persisted before
any redundant message is deleted. Failed cleanup blocks displacement reanchoring;
ambiguous send receipts remain fenced. No task completion is inferred from a
presentation merge.

## Behavioral test boundary

Control: real `DelegationCards.observe`, `reconcile`, restart deserialization,
queued flush and displacement. Observation: captured adapter transport calls and
persisted cards, not a standalone grouping helper. The canonical regression uses
the reported E / nested E.1 / G keys, refs, owner sessions and message receipts,
including the null/explicit-default profile split. Names and unrelated source
fields are omitted. It proves failed edits cannot delete extras, failed deletion
survives restart, union rendering preserves outcomes and no extra send occurs.

Additional cases exercise concurrent profile/topic-isolated dispatch, ambiguous
initial sends, sent/attempting reanchor receipts and stale queued anchor tasks.
Existing rendering and shared Telegram scheduler tests remain authoritative for
NBSP indentation, inline roles, heading-only bold and final-reply priority.

## Verification and lifecycle

Focused command:
`venv/bin/python -m pytest tests/gateway/test_delegation_card_consolidation.py tests/gateway/test_delegation_cards.py tests/gateway/test_delegation_card_anchor.py tests/gateway/test_delegation_card_reconciliation.py -q`

The two initial regressions failed on the base with two presentation keys and
four sends instead of three isolated conversation sends. Source changes then
passed the focused suite. Publication, promotion, activation and exact live
Telegram acceptance are separate receipts, not implied by these tests.

Rollback requires preserving the cards file and its cleanup ledger: reverting
code alone must not discard pending exact-message cleanup. Do not replay the
pre-consolidation cards file into a running gateway or guess Telegram message ID
mappings. Retire this overlay when the replacement preserves the full contract.
