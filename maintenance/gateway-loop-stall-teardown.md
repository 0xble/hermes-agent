# Gateway loop stall and teardown exit

**Patch identity:** `gateway-loop-stall-teardown`

## Invariants

- Async gateway handlers never take the synchronous session-store lock on the event-loop thread.
- Persisted model-override hydration reads occur in a worker and commit through the existing epoch/edit-pending compare-and-set fence.
- Session-store routing peeks reachable from async status, heartbeat, topic, webhook and API paths run off-loop.
- Gateway teardown keeps its immediate-exit contract: it does not wait for a default executor or lingering non-daemon worker after graceful teardown returns.
- New Telegram topic setup uses `AsyncSessionStore` for creation/title writes, `to_thread` for binding/reasoning sync writes, and async route resolution; it does not add a loop-thread store lock.

## Implementation

`GatewayTurnMixin._rehydrate_session_model_override_async` owns the async hydration path. It serializes with the per-session asyncio lock, calls `_load_session_model_override` through `asyncio.to_thread`, and uses `_commit_session_model_override` with the captured epoch. `/status`, `/model`, API route preparation and async model resolution use that path. Routing peeks in heartbeat ownership/polling, heartbeat restore, Telegram topic existence and webhook completion use worker threads.

`gateway.run.main` and `hermes_cli.gateway.run_gateway` use a private event loop whose `run_until_complete` returns directly to the hard-exit funnel, avoiding `asyncio.run`'s default-executor shutdown join. No drain, wait window or restart deferral was added.

## Verification

Focused RED/GREEN tests run through `HERMES_PYTHON=<worktree>/.venv/bin/python bash scripts/run_tests.sh <one file>` under `heavy-slot`: `tests/gateway/test_session_store_loop_boundaries.py` and the existing gateway exit tests. The test includes a real child process with a blocked default-executor job and a lingering non-daemon thread.

The live gateway CPU profile was read-only. `sample <pid> 5` showed the Python process dominated by provider/HTTP worker activity and broad concurrent thread contention rather than one clear gateway busy loop; no CPU change is included in this patch.
