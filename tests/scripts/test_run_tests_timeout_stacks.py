"""A real per-file timeout must preserve both kinds of stack."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _assert_timeout_stacks(tmp_path, *, forkserver):
    probe = tmp_path / "test_hanging_probe.py"
    probe.write_text(textwrap.dedent('''
        import asyncio
        import threading

        def blocked_worker_thread():
            threading.Event().wait()

        async def blocked_await():
            await asyncio.Event().wait()

        async def suspended_probe_task():
            await blocked_await()

        async def hanging_coroutine():
            task = asyncio.create_task(suspended_probe_task(), name="timeout-probe-task")
            await task

        def test_hang():
            threading.Thread(target=blocked_worker_thread, daemon=True).start()
            asyncio.run(hanging_coroutine())
    '''), encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/run_tests_parallel.py"), str(probe),
         "-j", "1", "--file-retries", "0", "--file-timeout", "5"],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
             "HERMES_TEST_FORKSERVER": str(int(forkserver))},
        capture_output=True, encoding="utf-8", errors="replace", timeout=25, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "runner_status=124" in output and "signal=SIGKILL" in output, output
    assert ("fork server: on" in output) is forkserver, output
    # The first report is a truncated preview; inspect the full failure output.
    _, header, stacks = output.rpartition("=== Python thread stacks before file timeout ===")
    assert header, output
    threads, section, tasks = stacks.partition("=== asyncio task stacks ===")
    assert section, output
    assert "in blocked_worker_thread" in threads, output
    assert "timeout-probe-task" in tasks and "in suspended_probe_task" in tasks, output
    assert "in blocked_await" in tasks, output


@pytest.mark.platforms("posix")
def test_per_file_timeout_dumps_stacks(tmp_path):
    _assert_timeout_stacks(tmp_path, forkserver=False)


@pytest.mark.platforms("linux")
def test_forkserver_per_file_timeout_dumps_stacks(tmp_path):
    _assert_timeout_stacks(tmp_path, forkserver=True)
