# Reviewed Cron Delivery

## Required Behavior

Patch identity: `reviewed-cron-delivery`.

Only jobs carrying a `review` declaration opt in, and they require `deliver: local`. Setting review also sets `output_local_only: true`. Other jobs retain their existing save, queue, delivery, silence, failure, and transcript behavior. CLI and cron tool edits preserve review unless explicitly replaced or cleared.

Resolve exactly 1 live, unended, unarchived, visible topic binding from the owning profile's `state.db`, by exact thread ID or exact session title. Exclude threads `255600` and `258219`. Recheck the binding before final send. Unresolved, ambiguous, ended, or changed subjects send nothing and upsert a normal cron incident.

The gateway control socket is the only review hand-in path. It injects an internal event with a reply capture, without a Telegram user message, typing indicator, or posted agent reply. Preserve human follow-ups for their own events. Control handlers run on executor threads, so submission must marshal onto the gateway loop. Routed profiles use the socket of the gateway serving them.

`PRESENT` selects the original. A `FIX` or successful `RERUN` selects new text only after the contract passes. Invalid replies, contract errors, busy subjects, gateway failures, invalid candidates, and timeouts use the original. Original violations remain visible in the receipt. A silent original skips review and delivery but records its disposition. Existing `[CRON_FAILURE]` routing remains outside successful report review.

Review defaults to 10 minutes from queue admission. At most 1 rerun gets a separate 20-minute budget, capped by the review deadline plus that budget. Reserve subprocess cleanup time and terminate the rerun tree on timeout. Held delivery runs outside cron execution slots and never blocks ordinary queue draining. Queue admission stays inside the fire ownership fence and before terminal run commit.

Final presentation uses the ordinary bot send path with the contract envelope intact, then mirrors the report as the subject agent's own assistant message. The profile-local `cron/review-receipts.jsonl` records run identity, decision, candidate checks, selected text, delivery outcome, message ID, and elapsed time.

## Ownership and Compatibility

The scheduler owns the decision state machine in `cron/scheduler_review.py`. `cron/review.py` is the single owner of the declarative contract checker. The installed output-guard's `cron_report` rule targets non-local delivery and prompt-derived presentation rules, so it cannot provide this local job's declarative contract API without a runtime plugin dependency. Contracts ship in the wheel through setuptools package data and can be overridden under the profile's `cron/contracts/`.

Relay is not involved. Its existing presentation path creates a user message, while the scheduler already needs the gateway's internal admission and captured reply. A relay wrapper would add an extra hand-in layer without changing that boundary.

The external router skips `output_local_only: true`. The lane converter excludes local delivery itself, but a pinned `failure_deliver` remains eligible, so rollout must also set failure delivery to local. Those owners stay in the scripts repository.

## Proof and Retirement

Run the checkout-owned test interpreter against `tests/cron/test_review*.py`, `tests/gateway/test_cron_review.py`, `tests/hermes_cli/test_cron_review.py`, and `tests/tools/test_cronjob_review.py`, plus the full cron suite and affected delivery, wake, and turn tests. Preserve the exact-head gate receipt. Source tests do not prove live gateway adoption or Telegram delivery.

Deploy only after independent review and authorized landing. Update Hermes and restart the gateway under the deployment owner's authority, opt in 1 report through supported job APIs, and read back its declaration and local lanes. Verify 1 final bot report, no user/review bubble, the exact session mirror, and the matching review receipt before expanding rollout.

Retire the fork patch only when upstream supplies equivalent opt-in contracts, invisible captured topic review, deterministic resolution, bounded fallback/rerun, and durable receipts, with behavior tests proving the same boundaries.
