# Activity-driven delegation-card anchoring

A conversation owns one **logical** delegation presentation; its physical Telegram
message may change. This supersedes the earlier strict same-physical-message rule.
Task IDs, internal display references and original task start times do not change.

## Symbol-first presentation

Only the heading text is bold: 🧵 **Delegating tasks** (the emoji is plain text).
Rows show `○ Task label · Role` for running, `◌` for queued, `✓` for returned,
`!` for failed/error/timeout and `Ⅱ` for interrupted/cancelled/budget exhaustion
or unproven recovered execution. Returned activity is `Awaiting parent`; failed
and interrupted activity remains explicit and awaiting parent, never success.
Unknown future states have an explicit unknown-status fallback, not a running dot.

Task labels stay authored and untruncated under the existing plain-text sanitizer;
roles remain inline. A second line, indented two nonbreaking spaces beyond its
row, shows `↳`, the compact tool emoji, and the bounded canonical tool
identifier only while running—no args, preview, or “Last tool”. Four
nonbreaking spaces per actual
parent layer preserve Telegram indentation, capped at three visible layers.
There are no visible numbering/ref prefixes or deep-parent ref markers, including
unlabeled legacy rows. Stable internal refs, parent identities and exact handling,
replacement, recovery and final-delivery receipts remain unchanged. Identical
labels never identify or merge tasks. Do not restore visible refs when maintaining
the lifecycle/tool API; they are separate concerns.

## Eligibility and transport

**Single rule:** while a visible row is `running`, move an active card only after
**six distinct ordinary messages in its exact adapter/chat/topic**, with no separate
elapsed-time eligibility condition. Shared API spacing and flood cooldowns remain
mandatory. Ordinary messages include received new messages and successfully sent
assistant replies, physical continuations and media. IDs only deduplicate them.
Status messages, card edits/replacements, edits, reactions, drafts, failed sends
and other topics do not count.
This is event-driven: there is no cooldown-expiry heartbeat, history polling, or
move for an idle or terminal-only card.

- Six pending observations saturate; recent ingress/reply IDs have a bounded
  1,024-entry deduplication window. Restart discards displacement, marks unproven
  execution unknown, and requires fresh activity. No inferred continued execution.
- Existing per-presentation lock/coalescing owns replacement. Telegram's existing
  expendable-send context, shared per-chat gate, 429 cooldown and final-reply
  priority own all send/edit/delete calls. There is no parallel transport queue.

## Replace and recover

Delete the exact old ID **before** sending a replacement. Persist
`delete_pending` → `deleting` before the delete request, `deleted` only after
confirmed success or known absence, then `sending` before the send request.
A short no-card gap is intentional; this is not atomic Telegram replacement.
Failed or ambiguous deletion never authorizes a send. Idempotent exact-ID deletion
may resume after restart, bounded to three issued attempts; scheduler deferrals
do not consume an attempt and all retries honor the shared flood deadline.

Render from fresh task state after deletion and again synchronously after the
send scheduler's waits, immediately before the Bot API request. The lifecycle
lock is not held over replacement transport waits. If no row remains running,
do not send. Completion/handling during the actual request is coalesced into a
follow-up edit or cleanup without changing task states, refs or handling receipts.

After confirmed deletion, a definite scheduler/429/API rejection may retry at the
shared deadline, up to three attempts. Ambiguous send, timeout or cancellation
retains `sending` across restart and later tasks in the same topic: no blind
resend. Telegram supplies neither an idempotency key nor a read-history API, so
an unreturned message needs exact operator reconciliation. A durable successful
`sent` receipt is adopted without resending. Existing send-first `attempting`
records remain fenced; existing send-first `sent` records retain exact-old cleanup.
The new phases must be reconciled before rolling back to a reader that lacks them.

Restart marks unproven execution unknown; recovering a deleted phase alone does
not infer active execution or resurrect a terminal-only card. A later genuinely
running task may reuse a confirmed-empty topic anchor, but never an uncertain send.

## Audited legacy dismissal while the gateway is running

The existing offline validator remains available; see
[exact-target reconciliation](delegation-card-reconciliation.md). Do not replace
`cards.json` while a gateway owns it.

For an explicitly authorized, individually reconciled historical target, stage
`$HERMES_HOME/cache/delegation/dismissal-request.json` with two fields:

- `snapshot_json`: the **literal UTF-8 JSON text** of the audited card snapshot;
- `manifest`: the existing `delegation-card-dismissal-v1` exact-target manifest,
  whose SHA-256 pins those exact snapshot bytes and contains operator,
  authorization, refs, full owner/source identity, message ID, reason and evidence.

On the next supported gateway startup, before unknown-state recovery, the manager
uses the same whole-batch validator and compares every target's current owner,
source, rows, message ID, handled proof and retirement state with the snapshot.
Any mismatch rejects the entire request. Unrelated current cards and changing
anchor render/revision fields are preserved, not overwritten from the snapshot.
Retirement and audit evidence are atomically persisted, then the request is archived
as `dismissal-applied-<request-sha256>.json`. Crash/replay after persistence is
idempotent. No timestamps, prose similarity or sibling successes infer handling.

This only dismisses obsolete **presentation**. Durable task outcomes, execution
rows and `handled` receipts remain untouched. A shared anchor with genuinely active
unrelated rows is kept and repainted, not blindly deleted. Do not restart solely to
force a request while another activation decision is pending; use the separately
authorized supported updater and durable postrestart handoff.

## Verification

`tests/gateway/test_delegation_card_anchor.py` exercises the real manager,
persistence, Telegram adapter and shared gate with only Bot API transport faked:
interleaved topics, deduplication, final priority, coalescing, terminal-only rows,
429, ambiguous/cancelled sends, delete failure, receipt-before-adoption restart,
late callbacks and final cleanup. Reconciliation tests cover exact-target mismatch,
concurrent unrelated work and replay. These tests are not a live Telegram receipt;
report live initial/replacement IDs, old absence and final cleanup separately.

### Coalescing

Replacement yields the lifecycle lock while transport is pending. Tests use the
public lifecycle methods during deletion, send acceptance and a final-priority
scheduler wait, then assert the resulting card without a later external event.
