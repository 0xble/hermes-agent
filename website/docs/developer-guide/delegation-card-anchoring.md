# Activity-driven delegation-card anchoring

A conversation owns one **logical** delegation presentation; its physical Telegram
message may change. This supersedes the earlier strict same-physical-message rule.
Task IDs, internal display references and original task start times do not change.

## Symbol-first presentation

The presentation starts directly with the first task, without a heading or blank
replacement line. An empty projection emits no text and is not sent to Telegram.
Rows show `○ Task label · Role` for running, `◌` for queued, `✓` for returned,
`!` for failed/error/timeout and `Ⅱ` for interrupted/cancelled/budget exhaustion
or unproven recovered execution. Returned rows have no activity subline; failed
and interrupted activity keeps the specific reason without “awaiting parent”.
Deferred rows show their detail alone, without a “Deferred” prefix. Running rows
without an observed tool also omit the subline, with no blank replacement line.
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

## Observed activity and reasons

The running subline prefers a currently bracketed runtime wait over the actual
canonical tool name. Process waits begin immediately before the registry blocks,
not merely because a process exists. Classified provider rate-limit/upstream-rate-
limit and overload retries bracket the existing interruptible backoff. All exits,
including timeout, interruption and exception, clear the matching wait token.
Nested/parallel waits preserve another still-active token; unrelated housekeeping
cannot erase one. Verified terminal billing/rate-limit failures retain only the
allowlisted reason, not raw errors, arguments, provider identity or retry text.

The child runtime owns each attempt and monotonic event sequence. The card rejects
late attempts/sequences, clears activity on terminal/resume/recovered unknown,
and never lets stale deferred detail mask resumed work. Reason events are identity
plus allowlisted enums, not an activity-report tool or model-authored phase. They
reuse the existing callback, ownership checks, persistence and display transport;
execution, retry outcomes, result retention and handling are not changed.

Intentionally omitted: delegation-pool capacity rejection (the runtime rejects or
runs inline, not waits); mere tracked-process existence/poll; unclassified errors;
unverified billing; guessed task phases, percentages, translations and timer ticks.
The Nous pre-call rate guard returns/falls back rather than waiting and currently
has no classified `failure_reason` result, so it does not acquire a fabricated wait
or terminal-reason label. No extra configuration knobs are required.

Label admission and child authoring use the same fixed policy: a top-level parent's
new labels allow 24 code points, then 20, 16, and a 12 floor at actual runtime depth.
The static schema stays at 24; no cached schema mutation. Historical same-row resume
labels and canonical profile-role names retain their identity unchanged.

Producer/relay/card tests: `tests/tools/test_delegation_wait_producers.py`,
`tests/gateway/test_delegation_activity.py`, and
`tests/tools/test_delegate_depth_labels.py`. Real local process waiting is exercised;
provider retry boundaries use deterministic doubles and make no provider requests.

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

## Terminal display TTL

Terminal rows remain in the durable projection and result/handling ledger, but their
card rendering expires independently. `display.delegation_terminal_ttl_seconds`
defaults to 300 seconds and accepts only positive integers; a platform `null`
override inherits the profile value and invalid values resolve to 300. The value is
frozen when every member of the original delegation call is terminal. The immutable
birth-call manifest retains complete membership across independent completion units
and nested owners; running, queued or not-yet-observed members prevent expiry.
The batch deadline is the latest member `terminal_at` plus the configured TTL.
Handling and delivery acknowledgments neither start nor shorten this display window.
Duplicate events and restart preserve the deadline. A validated resume reopens its
original batch until all members are terminal again; unrelated calls stay independent.
Legacy rows without birth-call metadata retain the per-attempt fallback: a valid
terminal timestamp derives a deadline once; missing timestamps hide immediately
without inventing history.

Expiry uses one coalesced scheduler per shared presentation scope. It deletes only
the visible status message after a confirmed, fenced adapter deletion; it never
retires rows, releases results, marks handling, or changes approvals/dispositions.
Active descendants keep expired terminal ancestors as context. Root-cap selection
runs before TTL pruning, so expired older roots do not backfill the visible window.

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
