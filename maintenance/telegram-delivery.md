# Telegram delivery: flood recovery, split sends, and emphasis

Load this unit when changing Telegram send, edit, or typing paths, the delivery ledger,
flood handling, or legacy-emphasis rendering. Rich rendering modes and paragraph spacing
are owned by [Telegram rendering](telegram-rendering.md).

## Required behavior

- One known flood window per chat is shared across text, edits, typing, uploads,
  rich/plain drafts, control prompts and deletions. Draft/control/delete calls recheck
  the window under the existing per-chat send lock. A new RetryAfter from those paths
  arms the same window, including timedelta values. Rich drafts do not fall back to a
  plain draft after a flood refusal. Draft/control results retain `flood_control` and
  `retry_after`; deletion returns false and retains cached status ownership for retry.
  These are reactive guards, not a durable deletion queue. Media rate limits are
  classified, but attachments are not durable redelivery obligations.
- Split sends resume after flood refusals; rejected deliveries back off and preserve
  recovery; a partial delivery does not trigger a duplicate fallback.
- Nested and multiline legacy emphasis is preserved.
- Synthetic gateway event IDs may identify notifications but never become Telegram reply
  anchors. Numeric Telegram message IDs still anchor ordinary DM-topic replies.

## Rate Boundary

The proactive text/interim-edit slot remains 1 second (up to 60 calls/minute).
The base typing loop has a separate default 2-second interval (up to 30/minute
per active loop), and final/overflow edits, drafts, media, control, deletion and
other direct Bot API paths are not all charged to one proactive slot. The sum is
therefore not bounded by this implementation, even before concurrent topic turns.
The 2026-09-27 follow-up adds no requests, retries, or producer frequency. It
closes known-window bypasses and removes duplicate helper definitions. It does
not claim a universal Telegram quota or a complete per-chat proactive budget.
Do not retire the broader scheduling work in #107612 on this evidence, or change
streaming/typing/icon preferences as a substitute for delivery correctness.

## Provenance and patches

- Fork patch identities: `slice-10-flood-coherence`, `slice-10-telegram-delivery`,
  `slice-10-telegram-delivery-followup`, `slice-10-delivery-ledger`,
  `slice-11-telegram-emphasis`, `telegram-delivery` (synthetic reply-anchor guard).
- Adopted upstream commits: split-send recovery `595f3a289c7`, partial-delivery suppression
  `969898d4ff4`, redelivery backoff `c961e5bb691` and `807435ac1ec`. All four are
  ancestors of upstream release v2026.9.24 (`f97608f178d1ffeca59860195ab7da295f7c8e5f`),
  verified on 2026-09-26. They are released native behavior, not pending borrowed
  changes. Separate local flood-coherence and ledger deltas remain. Flood coherence
  is tracked in [issue #107612](https://github.com/NousResearch/hermes-agent/issues/107612).
- Emphasis is an own contribution: [upstream PR 106906](https://github.com/NousResearch/hermes-agent/pull/106906),
  open at `37f872bad1706c6c50ccdccb825fc4d5ffd2c246` on 2026-09-19.

## Verification

`scripts/run_tests.sh` on `tests/gateway/test_telegram_flood_coherence.py`,
`tests/gateway/test_telegram_split_send_flood.py`,
`tests/gateway/test_delivery_flood_invariants.py`, the `test_delivery_ledger*.py` files,
and `tests/gateway/test_telegram_emphasis.py`. The synthetic anchor regression is
`tests/gateway/test_telegram_thread_fallback.py`; boot auto-resume injection and
numeric normal replies must both reach the Telegram fake transport.

## Retirement and rollback

The four adopted commits are already contained in v2026.9.24. Retire only their
redundant adaptation after checking the selected release against the regressions,
while preserving the independently required local deltas. Retire flood
coherence when upstream classifies media floods and shares one per-chat window. Retire
emphasis when PR 106906 merges and the candidate tag includes it. Roll back by reverting
the logical patch; no persistent data changes.
