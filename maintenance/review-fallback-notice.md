# Plugin-claimed subagent failure notices

Load this unit when changing the user-facing "Subagent failed" notice
(`gateway/run_turn_runner.py::_progress_subagent_notice`,
`tools/delegate_tool_progress.py::_ChildProgressRelay._on_complete`) or the
`subagent_failure_notice` plugin hook.

## Required behavior

- A child that ends `failed`, `error`, or `timeout` asks the
  `subagent_failure_notice` hook once, before any user-facing notice. A plugin
  that recovers the failure itself returns `{"action": "suppress"}`.
- The first relay records the decision on the relayed event as
  `failure_notice_claimed`. The gateway reuses it and asks the hook only for
  events that bypassed the relay, so one failure never runs the hook twice.
- Only presentation is suppressed. The child result, `subagent_stop`, and the
  parent's consolidated result are unchanged. No plugin, any other answer, or
  a hook error keeps the notice.
- A route owner's `max_retry_wait_seconds` (in the `credentials_cfg` passed to
  `delegate_task`) caps the provider cooldown a child sits out. A declared
  `Retry-After` longer than the cap ends the attempt at once as an exhausted
  retry (`agent/turn_api_error.py::exceeds_retry_wait_cap`). No cap, or no
  declared cooldown, leaves the normal retry policy unchanged.
- An owner that re-dispatches a finished background unit elsewhere calls
  `tools.async_delegation.supersede_delegation(old_id, new_id)` before the old
  unit's completion is queued (in practice from `subagent_stop`). The old unit
  is then persisted with `delivery_state='superseded'`: recorded, visible in
  the delegation list, never queued or replayed to the parent. Core refuses
  when the replacement is not an admitted delegation for the same parent
  (every routing field: session key, UI session, origin and parent session
  ids, gateway routing origin), when the old unit has more than one task (a
  sibling's result would be hidden), or when the old unit has already been
  reported. A refusal leaves normal delivery.

## Why

`review-candidate` retries a rate-limited reviewer on the next configured
route from `subagent_stop`, which runs after core has already announced the
failure. Each Fable 429 therefore told the user the review failed while the
fallback reviewer was still working and usually approved.

Hiding the notice was not enough: the failed attempt still re-entered the
parent as its own completion, so the parent woke up and narrated the rate
limit and the retry before the replacement's verdict arrived.

A rate-limited reviewer also honoured the provider's 600 s `Retry-After` three
times before it failed, so the fallback started about 30 minutes late. The wait
cap lets a route with a fallback behind it fail in seconds.

## Provenance

Fork patch identity: `review-fallback-notice`.

Upstream-owned code. No upstream issue or PR covered it when this patch landed.
The consuming plugin is `review-candidate` in the `agents` repository.

## Verification

Run `scripts/run_tests.sh tests/gateway/test_subagent_failure_notice.py`.
`TestClaimEvaluatedOncePerFailure` drives the real child relay into the gateway
`TurnRunner` and asserts one hook call whose first decision holds on both
surfaces.
`scripts/run_tests.sh tests/agent/test_retry_wait_cap.py` covers the wait cap
through the real conversation loop and `_build_child_agent`.
`scripts/run_tests.sh tests/tools/test_async_delegation_supersede.py` drives a
real background `delegate_task` whose `subagent_stop` hook re-dispatches and
supersedes it, and asserts the parent receives only the replacement.

## Retirement and rollback

Retire when upstream lets a plugin claim or defer a child failure notice and
supersede a background completion. To roll back, revert the patch commits;
`review-candidate` then only loses the suppression, not its fallback.
