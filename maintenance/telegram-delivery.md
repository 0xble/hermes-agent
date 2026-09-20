# Telegram delivery: flood recovery, split sends, and emphasis

Load this unit when changing Telegram send, edit, or typing paths, the delivery ledger,
flood handling, or legacy-emphasis rendering. Rich rendering modes and paragraph spacing
are owned by [Telegram rendering](telegram-rendering.md).

## Required behavior

- One flood window per chat is shared across edits, typing, and uploads. Media rate limits
  are classified, but attachments are not durable redelivery obligations.
- Split sends resume after flood refusals; rejected deliveries back off and preserve
  recovery; a partial delivery does not trigger a duplicate fallback.
- Nested and multiline legacy emphasis is preserved.

## Provenance and patches

- Fork patch identities: `slice-10-flood-coherence`, `slice-10-telegram-delivery`,
  `slice-10-telegram-delivery-followup`, `slice-10-delivery-ledger`,
  `slice-11-telegram-emphasis`.
- Adopted upstream commits: split-send recovery `595f3a289c7`, partial-delivery suppression
  `969898d4ff4`, redelivery backoff `c961e5bb691` and `807435ac1ec`. Flood coherence is a
  local patch with no upstream submission.
- Emphasis is an own contribution: [upstream PR 106906](https://github.com/NousResearch/hermes-agent/pull/106906),
  open at `37f872bad1706c6c50ccdccb825fc4d5ffd2c246` on 2026-09-19.

## Verification

`scripts/run_tests.sh` on `tests/gateway/test_telegram_flood_coherence.py`,
`tests/gateway/test_telegram_split_send_flood.py`,
`tests/gateway/test_delivery_flood_invariants.py`, the `test_delivery_ledger*.py` files,
and `tests/gateway/test_telegram_emphasis.py`.

## Retirement and rollback

Retire each adopted commit when the candidate upstream release contains it. Retire flood
coherence when upstream classifies media floods and shares one per-chat window. Retire
emphasis when PR 106906 merges and the candidate tag includes it. Roll back by reverting
the logical patch; no persistent data changes.
