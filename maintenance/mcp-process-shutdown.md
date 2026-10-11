# MCP Process Shutdown

Load when changing the MCP transport-error retry ladder or the shared interpreter-shutdown predicate.

**Patch identity:** `mcp-process-shutdown`.

## Required Behavior

An MCP daemon task must stop reconnecting when the process is already shutting down. CPython closes `ThreadPoolExecutor` workers during its thread-exit stage before `sys.is_finalizing()` becomes true. HTTP DNS on an established MCP session can then raise `cannot schedule new futures after shutdown` and exhaust the reconnect budget in a process that is already exiting.

The shared predicate reads the shutdown flag only from an already-loaded `concurrent.futures.thread` module. It never imports an executor during shutdown. Its existing optional exception-matching contract remains intact. MCP checks the predicate without an exception, so shutting down one local executor in a live process cannot silently suppress a real MCP failure. Both initial-connect and reconnect ladders stop before retry or park when process exit is independently known. The process-exit debug message and existing park warnings identify the emitting PID. Park deduplication still compares the original reason, and adds no log stream or request payload.

No credential, provider, model, configuration, schema or dependency change is involved.

## Provenance

The 2026-10-11 hook and MCP audit retained Mobbin and Pexafy parking warnings without a PID or traceback. Those individual historical processes cannot be attributed conclusively. A disposable genuine HTTP MCP handshake and tools/list against release `7e251dfadbb60093f8a42f75c0bf2858f30a7953` reproduced the exact five-retry warning during ordinary main return. The traceback crossed HTTPX2, HTTPcore, AnyIO, asyncio DNS and `ThreadPoolExecutor.submit`. The module-global and local executor shutdown flags were true while `sys.is_finalizing()` was false. A live-process negative control produced the same short error with the global flag false and remained correctly visible.

Upstream search on 2026-10-11 found related [issue #55924](https://github.com/NousResearch/hermes-agent/issues/55924), closed, covering cron teardown, and [issue #42466](https://github.com/NousResearch/hermes-agent/issues/42466), open, covering Hindsight retention. Open [PR #136704](https://github.com/NousResearch/hermes-agent/pull/136704) guards waiter cancellation on a closed loop, and open [PR #50070](https://github.com/NousResearch/hermes-agent/pull/50070) improves TUI-owned MCP teardown. These affect different boundaries and do not replace the observed executor-shutdown retry correction. No exact replacement was found in the searched open and closed issues and PRs.

Retire this patch when upstream demonstrates that ordinary CPython process exit cannot park or retry an established HTTP MCP connection, while local executor failures during a live process remain visible.

## Verification

`scripts/run_tests.sh tests/tools/test_mcp_process_shutdown.py tests/tools/test_mcp_cancelled_error_propagation.py tests/tools/test_mcp_park_log_dedupe.py tests/agent/test_interpreter_shutdown_turn_exit.py tests/cron/test_scheduler_shutdown_guard.py`

The process-exit regression creates a real DNS executor and returns from main without manually shutting it down. It verifies that MCP stops on the first error before `sys.is_finalizing()` changes. The negative control shuts down one real local executor and verifies the existing retry and warning behavior. Existing shared-predicate callers and task-cancellation regressions remain covered.

## Rollback

Remove only the `concurrent.futures.thread` state check in `tools/interpreter_shutdown.py`, the process-state guard, PID attribution and their imports in `tools/mcp_tool_server_run.py`, the focused regression file, this unit and its index row. The aggregate audit commit may contain unrelated fixes and must not be reverted wholesale.
