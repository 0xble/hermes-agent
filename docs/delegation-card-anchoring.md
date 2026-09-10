# Activity-driven delegation-card anchoring

A conversation owns one **logical** delegation presentation; its physical Telegram
message may change. This supersedes the earlier strict same-physical-message rule.
Task IDs, display references and original task start times do not change.

## Eligibility and transport

- At least one visible row must be `running` from actual lifecycle activity.
  Dispatched-only, returned, interrupted, unknown and handled rows do not qualify.
- Observe **eight distinct ordinary messages in that exact adapter/chat/topic**
  after its anchor. Count received new messages and successfully sent ordinary
  text replies. Do not subtract Telegram message IDs: IDs are only deduplication
  tokens. Status messages, edits, reactions, drafts, failed sends and other topics
  do not advance displacement. Media-only outbound sends conservatively do not
  advance it. This is an observed lower bound, not a complete Telegram history.
- At least **five minutes** since the last anchor and this process's tracking
  start. These small conservative defaults avoid treating every exchange as an
  invitation to move. There is no cooldown-expiry heartbeat or history polling:
  another actual conversation/lifecycle event must cause an eligible flush.
- Eight pending observations saturate; recent ingress/reply IDs have a bounded
  1,024-entry deduplication window. Restart discards displacement, marks unproven
  execution unknown, and requires fresh activity. No inferred continued execution.
- Existing per-presentation lock/coalescing owns replacement. Telegram's existing
  expendable-send context, shared per-chat gate, 429 cooldown and final-reply
  priority own all send/edit/delete calls. There is no parallel transport queue.

## Replace and recover

Persist an `attempting` fence before the send. A successful returned new-message
receipt is persisted as `sent` **before** adopting its ID. Only then does the old
ID become `obsolete_message_id` for bounded cleanup. There is no second replacement
while any member has outstanding old-message cleanup, or while send acceptance is
ambiguous. Failed replacement never deletes the original.

A definite scheduler/429 rejection may retry at the existing shared deadline.
Ambiguous send/timeout/cancellation remains fenced across restart and needs exact
operator reconciliation; Telegram supplies no idempotency key or read-history
API, so no automatic retry can safely prove the absence of an unreturned message.
A crash with a persisted successful receipt adopts that receipt without resending.
Already-deleted current anchors are not resurrected on restart.

Old cleanup uses the same three-attempt budget and scheduler deferral distinction
as normal cleanup. Exhaustion leaves the exact old ID visible in persisted state
and blocks further moves, rather than accumulating copies. Final parent delivery
continues to retire logical rows using exact receipt epochs/refs, then cleans both
anchors. Late tool activity cannot revive a recovered unknown or retired row.

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

### Coalescing follow-up review

The first source review of `3bef972de81345467982fd129b42e209fa9afb9d` identified
an early replacement return bypassing post-transport coalescing. The fix uses the
same success/cleanup/coalescing tail as normal sends and edits, including an empty
projection needing final cleanup. Regression callbacks arrive during both send
and old-anchor delete, with and without parent final delivery; no later external
event is supplied. Public lifecycle calls retain the shared lock, and a reentrant
accepted-mutation probe independently verifies the coalescing invariant. Review
confirmation is limited to this finding, the fix delta and its regressions; the
unchanged anchoring/administrative boundary retains the preceding review evidence.
