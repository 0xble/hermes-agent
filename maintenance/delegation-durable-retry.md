# Delegation durable retry

Load this unit when changing how a finished background delegation is classified,
retried, reported to the user, or pruned from `async_delegations`.

## Guarantee

Every still-authorized messaging-gateway background delegation ends with a durable settled outcome: a
completed result delivered to its parent, a bounded automatic retry, or a terminal `retry_state` and
`retry_reason` retained in `async_delegations`. Routine stops are not user notifications: failures,
exhausted budgets, retries the parent did not dispatch, and ended conversations settle quietly without a
gateway line or model turn. The sole direct user notice is a completed result whose original delivery
was permanently dropped (`result_undeliverable`) while its parent session is gone. No parent reply
(including `NO_REPLY`) can suppress that notice. Explicit user cancellation or revoked destination
authorization settles the obligation without delivery.

## Required behavior

- Messaging-gateway rows are inserted `retry_state='armed'` (`_persist_dispatch`).
  CLI/TUI/API-server origins have no retry driver and retain `none`, main's recovery
  behavior, and no automatic-recovery promise. When a managed row reaches a
  terminal state, `tools/delegation_resume.schedule_retry` classifies it once, inside a
  guarded `UPDATE ... WHERE retry_state='armed'`:
  - `none`: success, user stop (`stop_command`, `user_stop`, `/new`, session end),
    cancel, superseded, or stateless origin (cron/one-shot). Today's delivery path only.
  - `scheduled`: transient failure (`FailoverReason` rate_limit, upstream_rate_limit,
    overloaded, server_error, timeout, incomplete_response, upstream_blocked, or a 429/5xx/
    "cooling down"/fallback-exhausted text) or a lost owner (`unknown`, `interrupted`
    by shutdown, `stalled`).
  - `terminal`: everything else (auth_permanent, billing, content_policy_blocked,
    format_error, deterministic errors), any fan-out unit with more than one goal that
    did not fully succeed, and an exhausted budget.
- Backoff: `max(5m * 2^attempt capped at 60m, parsed reset + 30s)`. The reset comes from
  `agent.retry_utils.reset_delay_from_message` or a "resets at HH:MM" clock time.
- Budget per lineage: 6 replacements (`MAX_RETRY_ATTEMPTS`) or 24h from the root
  dispatch (`MAX_LINEAGE_AGE_S`). Lineage lives on the row (`retry_root`,
  `retry_attempt`, `retry_root_started_at`), so it survives restarts. Recheck the
  deadline at the due sweep, resume claim, durable dispatch admission, and actual
  pool submission; an expired queued replacement must not run or fall back inline.
- Dispatch is the enforced-notice mechanism. The gateway's async-delegation watcher
  calls `_drive_delegation_retries` on the orphan-sweep cadence. `sweep_retries`
  claims due rows atomically (`retry_claim` CAS) and returns actions:
  - A notice is injected as a parent turn telling it to call
    `delegate_task(action='resume')` and spawn with `recovery_context`. If the parent
    has not dispatched after `NOTICE_GRACE_S` (15 min), it is re-prompted once. If it
    still has not dispatched, the row turns terminal.
  - A routine terminal action has empty text. The gateway settles it `reported`
    without resolving a transport, waking the parent, or sending a notice; the
    durable `retry_reason` remains the record. A claim left `reporting` by a crash
    or an older build is reclaimed after `RETRY_CLAIM_TTL_S` and settles quietly.
  - A `result_undeliverable` action first classifies its parent with the shared
    async-completion target classifier. A live, idle-ended, or live
    compression-continued parent settles quietly. A compressed parent whose
    continuation has since ended is classified by that continuation's own end
    reason, the same way: idle settles quietly, a user boundary is gone. Only this
    report path does so; completion delivery keeps retrying an ended continuation,
    because it cannot inject into one. An unavailable session DB, a missing
    continuation, a compression-ended continuation, or an in-flight rotation stays
    owed with capped backoff. Only a missing parent or a user-boundary end (`/new`,
    user exit, session switch) of the parent or its ended continuation is gone. Then the line is
    sent straight to the user with `_deliver_platform_notice` and never passes
    through the model. Only an affirmative adapter delivery receipt settles it as
    `reported`; failed, missing, or privacy-skipped sends remain owed with 5m-to-60m
    capped backoff. Resolve the target and revalidate live authorization through the
    same helper as boot auto-resume notices. Authorization denial or an
    authorization-check failure settles it as `cancelled` with
    `authorization_revoked`, sends nothing, and logs suppression. A disconnected
    transport is not an authorization denial. The text is fixed:
    `Background task finished: <goal> — its result could not be delivered.` Apply
    the shared fail-closed egress scrub before truncating the goal and scrub the
    final line again before sending.
- Why not direct re-dispatch: rebuilding a child needs the live parent agent's
  credentials, toolsets and session context, which are not durable and must not be
  serialized. The notice path reuses every spawn gate. Automatic retry due prompts
  remain internal parent turns; ignoring both settles the retry quietly.
- `action='resume'` on a retry-owned row takes `claim_retry_dispatch` (single winner).
  The recovery brief heading `RECOVERY OF ... DELEGATION <id>.` links the new spawn
  inside `_persist_dispatch` (`link_replacement`). That marks the old row `dispatched`
  and carries `retry_attempt+1` to the replacement. Both `tasks=[{context: ...}]`
  and the legacy top-level context retain the effective child context at the durable
  dispatch boundary. The brief lists the prior live transcript paths. A consumed or
  cancelled managed brief is rejected in the same INSERT transaction; an unclaimed
  copied brief is admitted only as a fresh attempt-zero root and cannot link the
  original retry.
- Explicit stop producers retain a trusted cancellation category in child results,
  including units whose interrupted child makes their lifecycle status `error`.
  Shutdown/stall reasons remain system interruptions, not user cancellations.
- `action='stop'`, `/stop`, and session-ending stops also CAS still-owed durable
  retries to `cancelled`, clear their claims, and suppress their notices. The sweep
  records `retry_reason='stopped by user'`; if the row already owes a terminal
  settlement, that report is claimed and emitted once before it settles. The same
  session selectors as live interruption apply, with owner checks on model control.
  Gateway shutdown/lifecycle cuts preserve retry obligations, including legacy
  phase-specific reasons and unplanned shutdowns. Original completion/outbox
  delivery remains separate; its stale automatic-retry promise is recomputed.
- An accepted retry notice can wait queued behind a busy parent turn after the stop
  commits. Its wake carries `delegation_retry_notice` metadata (delegation id and
  notice claim), and every gateway-wake fence (admission, busy queue, strict pin,
  model boundary) drops it unless the row is still `noticing`/`notified` under that
  claim. A cancel or a newer notice claim retires the queued wake.
- Rows written before this unit (`retry_state='none'`) keep the legacy one-shot
  resume and boot notice. Boot notices skip retry-owned rows.
- Retention: `_RETENTION_OWED_SQL` exempts `armed/scheduled/noticing/notified/dispatching/
  terminal/reporting` retry rows and outstanding boot-notice claims from age and count caps. A
  legacy `resume_state='claimed'` no longer drops protection: that dropped protection is
  how deleg_a11d4b48 was pruned. `_prune_durable_records` first classifies armed rows,
  so plain successes leave the protected set immediately.
- Terminal ownership is shared with the durable failure-delivery outbox. Classification
  follows an owned terminal event, including a recovered terminal-fallback event when
  the lifecycle row has no `event_json`. Retry sweeps wait until the original
  lifecycle/outbox delivery settles, preserving its exactly-once replay rather than
  racing it with a retry notice or final report. Replayed owned events carry the
  durable retry note at consumer claim time.
- The failure-delivery outbox still routes original completions to the parent. Do not
  add a direct user line for retry-budget, non-dispatch, ended-conversation, or
  non-retryable failures: the user explicitly hid routine background-task-stopped
  notices. Keep the gone-parent `result_undeliverable` notice as the narrow exception.
- Queued shutdown keeps its `interrupt_all` owner when cancellation races the
  dispatch insert. Its durable outcome is `interrupted` and retryable, not user
  `cancelled`.
- Visibility (R5): the completion event carries `retry_note` ("Hermes will retry this
  automatically at HH:MM ... Do not re-dispatch it yourself", or a plain no-retry note
  without promising a user notice), and `action=list` shows
  retry-pending rows as `status=retry_<state>`.

Fork patch identity: `delegation-durable-retry`.

## Tests

`tests/tools/test_delegation_retry.py`, `tests/tools/test_delegation_retry_controls.py`,
`tests/gateway/test_delegation_retry_delivery.py`, `tests/tools/test_delegate_control_actions.py`,
`tests/gateway/test_delegation_auto_resume.py`.
