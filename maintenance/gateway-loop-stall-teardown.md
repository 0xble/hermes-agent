# Gateway loop stall and teardown exit

**Patch identity:** `gateway-loop-stall-teardown`

## Invariants

- Async gateway handlers never take the synchronous session-store lock on the event-loop thread.
- Persisted model-override hydration reads occur in a worker and commit through the existing epoch/edit-pending compare-and-set fence.
- Session-store routing peeks reachable from async status, heartbeat, topic, webhook and API paths run off-loop.
- Gateway teardown keeps its immediate-exit contract: it does not wait for a default executor or lingering non-daemon worker after graceful teardown returns.
- New Telegram topic setup uses `AsyncSessionStore` for creation/title writes, `to_thread` for binding/reasoning sync writes, and async route resolution; it does not add a loop-thread store lock.

## Implementation

`GatewayTurnMixin._rehydrate_session_model_override_async` owns the async hydration path. It serializes with the per-session asyncio lock, calls `_load_session_model_override` through `asyncio.to_thread`, and uses `_commit_session_model_override` with the captured epoch. `/status`, `/model`, topic model selection and async model resolution use that path. The model-switch context does not rehydrate synchronously again when no persisted override exists. Both typed and picker `/fast` writes and route warnings run off-loop. API session chat, completions, responses and run creation offload route-conflict validation, including its synchronous override lookup. Routing peeks in heartbeat ownership/polling, heartbeat restore, Telegram topic existence and webhook completion use worker threads.

`gateway.run.main` and `hermes_cli.gateway.run_gateway` use `gateway.event_loop.run_until_teardown`, a private event loop whose `run_until_complete` returns directly to the existing hard-exit funnel, avoiding `asyncio.run`'s default-executor shutdown join. No drain, wait window or restart deferral was added.

## Verification

Focused RED/GREEN tests run through `HERMES_PYTHON=<worktree>/.venv/bin/python bash scripts/run_tests.sh <one file>` under `heavy-slot`: `tests/gateway/test_session_store_loop_boundaries.py` and the existing gateway exit tests. The test includes a real child process with a blocked default-executor job and a lingering non-daemon thread.

The new-topic first-turn audit found session creation and turn markers already use `AsyncSessionStore`, bindings use the async DB facade / `to_thread`, and reasoning settings use the existing off-loop applier. Title callback attachment happens in `GatewayTurnRunner`'s synchronous worker; title/icon generation is not an event-loop-side prerequisite for launching the agent. Renames and icon persistence cross async/off-loop boundaries. The guarded first-topic setup test covers route creation, binding and session-open preparation; it does not reproduce the historical 5–7-minute delay under load.

The existing live gateway CPU samples were read-only. The three native `sample` captures show 405, 411 and 494 threads with dominant condition/GIL waits and I/O waits; the middle capture sampled all 110 main-thread stacks reacquiring the GIL after `kqueue`. That supports thread/GIL contention, not a proven gateway busy loop or an identified provider hot function. The samples do not identify the exact source of sustained >1-core CPU; no CPU change is included.
