# Async Completion Source Convergence

Load this unit when changing completion routing, terminal delivery dispositions, or watcher retry batches. Read [ledger ownership](async-delegation-ledger-off-loop.md) before changing settlement.

## Required Behavior

An async completion with no messaging source, raw API session, or nonempty `session_key`, `parent_session_id`, `platform`, `chat_id`, or `user_id` is terminal. Claim through `claim_event_delivery`, drop the exact claimed lifecycle or outbox row off-loop, and log its `delegation_id` and reason only after settlement succeeds. Its result remains queryable.

Routing hints, disconnected adapters, unavailable owner/session stores, and unavailable API delivery remain retryable without spending an unadmitted attempt. A terminal classification is not a settlement receipt: a failed claim or SQL drop must leave that event in the retry batch. Remove settled siblings from partial-failure retry batches. Preserve shared authorization, cancellation shielding, profile context, and event identity.

Boot notices use the existing shared notice-target gate and auto-resume trigger claim/consume path. No-route notices are consumed instead of requeued. Plain process completions retain `None` for terminal suppression and `False` for retry.

## Source And Adoption

Fork patch identity: `async-completion-unresolvable-drop`. This adapts the older proposal to fork main `9c465b1d10085f3d57ce33599f58ca84096be1c2`, whose shared off-loop ledger and notice-authorization owners must not be bypassed. No schema or live-state migration is required.

Upstream main `dc0fe3e476a06cfa9c0cb289f3604b0f80bc2734` retains boolean completion readiness. [PR 141926](https://github.com/NousResearch/hermes-agent/pull/141926) addresses related retry-budget loss, not this route-free settlement distinction. Retire this divergence only after a selected upstream release preserves the terminal/retry distinction and passes the proofs below.

## Verification And Rollback

Run `scripts/run_tests.sh tests/gateway/test_completion_delivery.py tests/gateway/test_delegation_auto_resume.py` with isolated homes and the selected test interpreter. The regressions cover normal watcher consumption, actual SQLite drop failure and retry, mixed retry groups, exact outbox identity, partial route hints, raw API dispatch, off-loop settlement and cancellation.

A simultaneous failed drop and failed defer/refund still requires shared claim-lifecycle recovery: the next claim can report the existing lease as owned and consume an unsettled queue copy. Do not treat the focused drop-only recovery tests as proof of that stronger guarantee.

Rollback is a source revert. Do not repair live delivery rows as part of this patch.
