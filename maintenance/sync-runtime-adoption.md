# Gateway runtime adoption

Fork patch identity: `sync-runtime-adoption`.

## Required behavior

The messaging gateway uses upstream's `SessionExecution` agent acquisition, including its delegation-mode build scope and locked-runtime handling. `TurnRunner` binds the fork's agent lifetime in its fresh-agent construction hook, without duplicating acquisition. Cached sessions keep their pinned delegation schema.

Gateway runtime metadata uses `hermes_cli.runtime_provider_custom.session_provider_identity`. A selected custom entry retains its name while it serves the current endpoint. If that entry moves, resume uses the endpoint's remaining owner or the bare adapter, never the moved entry's new key with the old URL.

A claimed messaging turn stopped by `/new` cannot resolve onto the fresh conversation after the route moves. Pending and already-delivered stops are generation-bound. A compression continuation remains the same logical conversation, and a route change without Stop retains normal resume behavior. This check runs off-loop before turn preparation.

## Verification

Contract tests: `tests/gateway/test_gateway_delegation_mode.py`
  `tests/gateway/test_named_custom_provider_session_identity.py`
  `tests/gateway/test_messaging_new_drops_claimed_turn.py`

These tests drive real parent/child inference against a loopback provider and real persisted session metadata. They retain the heartbeat/cache pin, explicit mode, stop propagation, shared endpoint and moved-entry cases.

The control-watcher runner also binds the production stopped-claim route check.
Its real outbox/injector tests and `test_goal_continuation_identity.py` must reach
strict session and goal validation rather than failing at a missing mixin method.

## Retirement and rollback

Retire when upstream runtime acquisition and metadata sync cover the fork contracts directly. Revert the adaptation together with any consumer of the fork lifetime binding, never restore a duplicate of the upstream acquisition method.
