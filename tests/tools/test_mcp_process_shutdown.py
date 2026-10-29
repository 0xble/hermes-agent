"""MCP stops retrying only when process teardown is independently known."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from tools.interpreter_shutdown import interpreter_shutting_down
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_server_run import _RetryBudget


def test_real_process_exit_stops_mcp_retry_before_sys_finalizing(tmp_path):
    """CPython shuts down its pools before sys.is_finalizing becomes true."""
    root = Path(__file__).resolve().parents[2]
    code = r'''
import asyncio
import concurrent.futures
import concurrent.futures.thread as pools
import json
import os
import sys
import threading
sys.path.insert(0, sys.argv[1])
os.environ["HERMES_HOME"] = sys.argv[2]
from tools.interpreter_shutdown import interpreter_shutting_down
from tools.mcp_tool import MCPServerTask
from tools.mcp_tool_server_run import _RetryBudget

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
async def warm_dns():
    await loop.getaddrinfo("localhost", 80)
asyncio.run_coroutine_threadsafe(warm_dns(), loop).result(timeout=5)
release = threading.Event()
blocker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
blocker.submit(release.wait, 10)
server = MCPServerTask("exit-probe")
server._ever_connected = True
async def no_backoff(_):
    pass
server._backoff_sleep = no_backoff
async def after_exit():
    try:
        while not pools._shutdown or not loop._default_executor._shutdown:
            await asyncio.sleep(.005)
        try:
            await loop.getaddrinfo("localhost", 80)
        except RuntimeError as exc:
            keep_running = await server._on_transport_error(exc, _RetryBudget())
            print(json.dumps({"error": str(exc), "keep_running": keep_running,
                  "process_shutdown": interpreter_shutting_down(),
                  "sys_finalizing": sys.is_finalizing(),
                  "retries": server._reconnect_retries}), flush=True)
    finally:
        release.set()
asyncio.run_coroutine_threadsafe(after_exit(), loop)
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(root), str(tmp_path)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    observed = json.loads(result.stdout.strip())
    assert observed["error"] == "cannot schedule new futures after shutdown"
    assert observed["sys_finalizing"] is False
    assert observed["process_shutdown"] is True
    assert observed["keep_running"] is False
    assert observed["retries"] == 0
    assert "parking" not in result.stderr


def test_live_local_executor_shutdown_still_retries_and_warns(caplog):
    """The short error alone is insufficient to classify a process as exiting."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pool.shutdown(wait=True)
    with pytest.raises(RuntimeError) as error:
        pool.submit(lambda: None)
    assert interpreter_shutting_down() is False
    server = MCPServerTask("live-executor")
    server._ever_connected = True
    server._backoff_sleep = AsyncMock()
    server._park_and_rearm = AsyncMock(return_value=False)

    async def drive():
        with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
            for _ in range(5):
                assert await server._on_transport_error(error.value, _RetryBudget()) is True
            assert await server._on_transport_error(error.value, _RetryBudget()) is False

    asyncio.run(drive())
    assert server._backoff_sleep.await_count == 5
    server._park_and_rearm.assert_awaited_once()
    assert "cannot schedule new futures after shutdown" in caplog.text
    assert "parking" in caplog.text
    assert f"pid={os.getpid()}" in caplog.text


@pytest.mark.parametrize("ever_connected", [False, True])
def test_known_finalization_exits_both_connect_ladders(ever_connected, monkeypatch, caplog):
    server = MCPServerTask("finalizing")
    server._ever_connected = ever_connected
    server._backoff_sleep = AsyncMock()
    monkeypatch.setattr(sys, "is_finalizing", lambda: True)
    with caplog.at_level(logging.DEBUG, logger="tools.mcp_tool"):
        keep_running = asyncio.run(server._on_transport_error(ConnectionError("closed"), _RetryBudget()))
    assert keep_running is False
    server._backoff_sleep.assert_not_awaited()
    assert server._reconnect_retries == 0
    assert f"pid={os.getpid()}" in caplog.text
    assert "parking" not in caplog.text
