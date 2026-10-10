# Session controls

## Identity

- **Fork patch identity:** `session-controls`

Session controls let one Hermes session pause, resume, clear, or replace another session's goal, and pause, resume, or stop another session's loop. A fresh verbatim quote from the requester's latest typed message authorizes the action; otherwise the durable request waits for an allowlisted Telegram Approve/Deny press.

Agents reach this primitive through the `goal_set` and `loop_set` tools, which the `goal-lifecycle` and `loop-lifecycle` plugins in the agents repository register. Those plugins call `apply_control` (and `resolve_target`) when a control action carries `target` or `user_quote`; core itself registers no tool, so a build without those plugins has no initiating entry point.

## Required behavior

- Store one audit/outbox record per control under `state_meta` with a 24-hour pending expiry.
- Resolve only bare session IDs or `hermes:<current-profile>/<session-id>` targets; reject unknown and cross-profile targets.
- Treat relay and gateway-authored rows as non-user text. Quotes must be whitespace-normalized, at least 12 characters, present in the latest typed user message after the gateway `[Replying to…: "…"]` pointer is stripped, and drawn from a source message no longer than 4,000 characters.
- Refuse a quote (`user_quote_negated`) when it contains a negation token (`not`, `don't`, `never`, `no`, `shouldn't`, …) or one appears within the three words before it. `stop` is a control verb, not a negation. A refused quote (stale, paraphrased, relayed, from another session, or negated) never applies the control: `apply_control` creates the same Approve/Deny request it creates without a quote and reports the refusal as `quote_refused`.
- Residual risk: quote authority is a verbatim substring, not an interpretation of intent. It is mitigated by using only the latest typed message, the negation check, the full source message shown in the target-topic notice, and Approve/Deny buttons as the fallback.
- Goal resume applies only to a `paused` goal; done, cleared, and already-active goals return `nothing_to_resume`. Pause, clear, and replace require an active or paused goal.
- Button requests are created only for targets whose session source has an Approve/Deny surface (Telegram); others return `target_unapprovable`. The base adapter's text fallback remains for safety, and the quote path still works for any routable target.
- A replacement records `actor="user"` with `authority` `quote` or `button` (plus `approved_by`), so the judge and continuation present the old goal as superseded. Replace notices show `old -> new`.
- Use the existing GoalManager and LoopManager methods for mutations. Goal replacement preserves revision history and refuses without explicit authority.
- Telegram callbacks must pass the existing callback allowlist and resolve through a durable compare-and-set; a second, expired, or unauthorized press must not mutate state.
- Gateway delivery is profile-scoped and drains durable request/outcome rows after restart. The watcher
  runs SessionDB and manager work in the gateway executor and expires requests with an in-transaction
  CAS. Interrupted approvals are not retried: `applying` rows older than 10 minutes become
  `failed` with error `interrupted`, and the mutation may already have happened. It
  marks undeliverable CLI/TUI rows skipped/done rather than growing the outbox forever. The watcher drains only the launch
  profile's SessionDB and `session_store`; it does not discover or drain another profile's store.

## Continuation lifecycle fence

Every gateway goal continuation is stamped by the shared synthetic-event constructor with its
session ID, authored-definition fingerprint, and creation time. The fingerprint reuses the control
card definition identity (creation time plus objective, contract, criteria, gates and revisions),
not progress counters or the active/paused status. Wake admission, idle ingress (including ordinary
non-internal continuations), busy ingress and FIFO follow-up all require the goal to be active and
the stamped definition to match. Resolved-session ingress also compares the stamp with the actual
session ID after routing and Telegram binding recovery, so a /new or /resume cannot move an old
continuation into the replacement conversation. Control wakes use the existing strict gateway
session pin. Unstamped legacy continuations fail closed.

Restart-persisted control outbox records use that same check, retaining their durable discard
receipt. An applied historical pause/clear only removes queued continuations created no later than
the control, leaving a later resume's continuation and human queue items intact. A busy session or
arbitrary queue depth is not an enqueue receipt: the outbox retries until its own continuation is
accepted or superseded. Queue cleanup runs synchronously on the event loop and removes the adapter
slot only while it still holds the inspected continuation. A persisted gateway origin with an
unavailable adapter retains its unsettled outcome flags for retry, including cleanup and notices;
only a missing origin is permanently skipped. Offline continuations still record supersession,
and the existing 24-hour outcome expiry bounds retries.

## Proof surface

Core regression tests cover quote freshness, relay rejection, target resolution, immediate controls, pending approval, atomic resolution/expiry, goal replacement, revision isolation, and loop actions. Gateway tests cover the non-blocking watcher/outbox behavior, Telegram-topic metadata, route skipping, continuation cleanup, and goal-resume admission. Telegram tests cover callback authorization, callback rendering that preserves the request text, and no-op second presses.

## Upstream disposition

The `hermes_cli/session_controls.py` core primitive, `GoalManager.replace`, gateway outbox watcher, base adapter fallback, and Telegram `ctl:` buttons are upstream candidates. The fork patch remains local until the S1 review and landing path is authorized.
