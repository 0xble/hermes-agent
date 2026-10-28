# Goal status notice flood retry

## Required behavior

Goal status notices (`⏳` parked, `↻` continuing, `▶` resumed, `✓`
achieved, `⏸` paused, and blocked) must not be silently lost when the delivery
adapter refuses a send with a short `flood_control:<seconds>` window. The first
send remains on the normal path; only the bounded recovery wait runs off the
post-turn callback and turn pipeline. Longer windows and exhausted retries emit
one warning naming the notice kind. This patch does not add a durable notice
outbox: the existing durable outbox owns final turn responses and explicitly
excludes unrelated background notifications, so the bounded retry and terminal
warning are the documented recovery boundary for notices.

## Protected meaningful outcome classification

The shared goal notice boundary classifies achieved, blocked, genuine pause,
failure/exhaustion and resumed-control receipts as existing `OUTBOUND_FINAL`;
routine parked, continuing, wait-age and generic status remain
`OUTBOUND_NOTICE`. Initial send, deferred post-delivery send and background retry
use the same existing goal-kind inference. The actual message outcome wins over
an incidental caller hint: a six-hour blocker pause arrives on the idle
`wait-age` path but is a meaningful paused outcome. User slash-control replies
continue through their ordinary protected reply path.

This protects meaningful notices from local daily-quota shedding without
changing the Telegram adapter, quota ceiling, ledger, limiter, retry count,
cumulative flood-wait budget, route metadata or task cleanup. Protection is not
a guarantee of external delivery: transport failures retain the same bounded
retry and warning boundary, not a new durable goal-notice outbox.

On fork base `3b2751ca843540885e157dc918877c4a3a841100`, seven meaningful
message kinds and two deferred outcome cases were shed before the mocked Bot API
under real isolated `DailyQuota` pressure (11 message-creation attempts versus
a soft ceiling of 10). The same cases pass with protection; routine notices
remain shed. Real GoalManager/CAS-produced achieved, blocked and six-hour paused
outcomes also reach the real Telegram send path at that pressure. Deferred
achieved/blocked retries retain protection and thread metadata after a real
short flood refusal. Only the judge result and network transport are controlled;
this is local integration coverage, not live Telegram E2E.

The companion [quiet unchanged-wait repair](goal-external-wait-backoff.md)
records pinned upstream guidance, prior-art disposition and the focused RED/GREEN
proof. Implementer: Hermes subagent, `gpt-6.1-sol` through
`custom:openai-proxy`. Parent owns independent exact-candidate review, exact-head
portable/hosted qualification, integration and live activation. Existing patch
identity, rollback and retirement remain in force; retire classification only
when upstream passes the same meaningful-versus-routine quota tests.

## Independent hypothesis

The loss occurs in `gateway/run_goals.py`: `_send_goal_status_notice` logs a
failed `SendResult` and returns, while `_defer_goal_status_notice_after_delivery`
awaits that send from the post-delivery callback. The narrow complete correction
is to classify the shared `flood_control` error, reuse the short-window helper
and thresholds from cron fix #290, then schedule a tracked background retry
rather than sleeping inside the callback. Keep non-flood failures and long or
exhausted flood windows visible in one warning with a stable notice kind. Share
the helper in `gateway.delivery_ledger` so cron and goal notices cannot drift.

Alternatives rejected: sleeping directly in the callback would delay the next
turn; falling back to another sender would spend traffic inside the known flood
window; a new goal-specific durable outbox would duplicate the final-response
ledger and require restart/replay semantics not present for notices.

Regression shape: a short flood refusal returns from the first notice send,
waits in a tracked task, retries successfully, and leaves the callback free;
a long refusal logs once without retrying; repeated short refusals stop after a
bounded attempt count and log once. Existing cron #290 regressions must retain
the same 15-second cumulative budget and 0.5-second slack.

## Patch

**Patch identity:** `goal-notice-flood-retry`.

**Source surfaces:** `gateway/run_goals.py` and the shared flood helper in
`gateway/delivery_ledger.py`; `cron/scheduler_delivery.py` now imports the same
helper and thresholds instead of carrying a second copy.

**Proof surface:** `tests/gateway/test_goal_status_notice.py` plus the existing
cron live-delivery confirmation and goal verdict/parked-wake suites.

**Upstream prior art:** The request names fork commit `7b78199d51` (#290), which
is the covering implementation and was inspected before adaptation. No separate
upstream search is required for this fork-only adaptation; any later search
results will be recorded here before landing.

**Rate budget:** no new steady-state notice producer or call type. A failed
notice gets at most two additional send attempts (three total attempts) and only
for a cumulative short-flood wait within 15 seconds; the retry task is tracked
outside the turn pipeline. The retry replaces a notice that was otherwise
silently dropped. It can still contend for the shared per-chat Telegram budget,
so it is bounded and never runs after the short window budget or attempt cap.

**Retirement:** remove this fork implementation when a selected upstream release
retries goal status notices with equivalent off-hot-path bounded recovery and
passes the proof surface.

**Rollback:** revert the logical patch in one commit; no schema, config, or
persistent-state migration is introduced.
