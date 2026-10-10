# Async completion source convergence

Load this unit when changing gateway completion routing, async-delegation delivery claims, or the completion watcher retry path.

## Required behavior

- An `async_delegation` completion with no resolvable messaging source and no raw API session is consumed once, logged once with its `delegation_id` and reason, and its durable delivery row is marked `dropped`.
- A transiently unavailable adapter, session store, or API delivery path remains retryable and returns the event to the completion queue.
- A raw API session id continues through the API dispatcher instead of being treated as an unresolvable messaging event.
- Auto-resume notices with no route are terminally consumed rather than requeued forever. Plain process-completion events already use `None` from their injection seam for terminal no-route drops; only `False` is requeued.

## Why

Leaked test rows with empty routing metadata entered a live `state.db`. The gateway completion watcher treated an unresolvable source exactly like a transient delivery failure, requeued the same event every tick, and emitted a warning indefinitely.

## Provenance

Fork patch identity: `async-completion-unresolvable-drop`.

## Verification

Run `scripts/run_tests.sh tests/gateway/test_completion_delivery.py` with an isolated `HERMES_HOME`. The regression coverage asserts terminal ledger disposition, transient requeue, and raw API dispatch routing.

## Retirement and rollback

Retire when the selected upstream release distinguishes terminally unresolvable completion routes from retryable delivery failures and carries the regression coverage. To roll back, revert the patch commit; no live state or configuration migration is required.
