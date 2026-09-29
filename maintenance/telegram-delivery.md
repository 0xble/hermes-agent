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
- The full server `retry_after` is enforced for rich sends and edits, typing, and
  the other guarded routes across adapter replacement and gateway restart.
- Split sends resume after flood refusals; rejected deliveries back off and preserve
  recovery; a partial delivery does not trigger a duplicate fallback.
- Nested and multiline legacy emphasis is preserved.
- Synthetic gateway event IDs may identify notifications but never become Telegram reply
  anchors. Numeric Telegram message IDs still anchor ordinary DM-topic replies.

## 2026-09-28 rich egress flood incident: independent diagnosis

At 21:30 PDT the live Telegram API returned a roughly five-hour `RetryAfter` while inbound
events and response generation continued. The rich final-send route returns a transient
`SendResult` carrying that wait but never arms the adapter's shared per-chat flood window.
Repeated rich sends therefore make more API calls during the known penalty. The existing
window also caps enforcement at 300 seconds and exists only in memory, so other routes
would resume too early or immediately after a gateway restart. The exact request that
first crossed Telegram's quota is not established by the logs.

**Patch boundary:** A structured flood refusal from rich send, rich edit, typing,
legacy final edit, or overflow continuation must arm the same per-chat window as
legacy sends, drafts, and media. No formatting fallback may spend another request
inside a known penalty. The guard must enforce
the complete server deadline across adapter replacement and gateway restart. A refusal
must return `flood_control:<remaining seconds>` so the delivery ledger defers final
reply recovery. Read timeouts and ambiguous network failures retain the no-resend rule.
An accepted rich send must never trigger a legacy fallback.

**Alternatives:** Reducing progress output lowers load but cannot enforce a published
deadline. Falling back to MarkdownV2 immediately after a 429 spends another request
inside the penalty. Keeping only an in-memory cooldown still loses the deadline on
restart. A new generic retry framework is unnecessary for this adapter-owned contract.
The narrow solution is a durable, profile-scoped deadline and reuse of the existing
fail-closed result across every guarded egress path.

**Upstream comparison (2026-09-28):** [#46762](https://github.com/NousResearch/hermes-agent/issues/46762)
reported the rich-send retry gap and is closed. [PR #47307](https://github.com/NousResearch/hermes-agent/pull/47307)
is open and proposes immediate Markdown fallback after a rich 429. That addresses a
rendering symptom but conflicts with the server's multi-hour wait in this incident.
[#107612](https://github.com/NousResearch/hermes-agent/issues/107612) tracks the wider
outbound budget gap and remains open. No released equivalent to this full-deadline,
restart-surviving guard was found in the inspected current fork/upstream paths.

**Regression:** A rich 429 with a long wait must prevent another rich or plain send,
including from a newly constructed adapter using the same profile; an unrelated chat
must remain usable, and a completed deadline must expire. Failed rich edits must
enforce the same window. No new Bot API calls are part of the correction. A small
profile-local deadline record is additive; rollback can ignore it. Platform scope is
Telegram on every Hermes host. Whether Telegram applies a given penalty to one chat or
the whole bot remains uncertain, so this patch follows the existing per-chat contract.

**Patch identity:** `telegram-retry-after-egress`. Source surfaces:
`plugins/platforms/telegram/adapter.py` and `flood_state.py`; proof surface:
`tests/gateway/test_telegram_flood_coherence.py` and the adjacent split-send/rich
tests. SQLite stores the deadline in the profile; an atomic per-chat file preserves
it if a SQLite write fails. A storage read error refuses requests in 60-second
increments until the store can be read. If all profile writes fail, only the
current process retains the deadline and durability is degraded. Retire when an
upstream release enforces full rich and typing RetryAfter
deadlines across restart without duplicate sends. Roll back this patch's adapter
classification, persistence calls and `flood_state.py` together, leaving the
earlier flood-coherence routes intact; the additive deadline DB can remain unused.

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
The 2026-09-28 correction also adds no requests and suppresses known-window
traffic. Outside a penalty, the configured 1-second send/edit slot permits up
to 60/minute and the independent 2-second typing loop permits 30/minute per
active loop: 90/minute with one loop, above the roughly 60/minute private and
20/minute group envelopes. More concurrent loops increase that sum. The wider
budget in #107612 remains necessary to prevent the first rate-limit event.

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
