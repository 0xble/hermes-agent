"""A runaway local cell loses only its own kernel, including its descendants."""
import json
import logging
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from tools import code_kernel as kernels


@pytest.fixture(autouse=True)
def isolated_kernels(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kernels, "_resolve_owner", lambda task: task)
    monkeypatch.setattr(kernels, "_ensure_background_reaper", lambda: None)
    monkeypatch.setattr(kernels, "_sweep_stale_staging_dirs", lambda: None)
    kernels.shutdown_all_kernels()
    yield
    kernels.shutdown_all_kernels()


def call(code, owner="memory-test"):
    return json.loads(kernels.execute_in_session_kernel(
        code, task_id=owner, mode="strict", child_python=sys.executable, child_cwd="",
        sandbox_tools=frozenset(), timeout=10, max_tool_calls=1, reset=False,
        is_interrupted=lambda: False,
    ))


@pytest.mark.parametrize("setting,expected_mb", [
    (None, 8192), (200, 200), ("256", 256), (0, 8192), (-1, 8192),
    (True, 8192), ("invalid", 8192), (float("inf"), 8192),
])
def test_configured_ceiling_retires_only_over_budget_kernel(monkeypatch, tmp_path, caplog,
                                                           setting, expected_mb):
    from hermes_cli.config import atomic_config_write
    from tools import code_kernel_memory as memory

    config = {} if setting is None else {"code_execution": {"kernel_memory_limit_mb": setting}}
    atomic_config_write(tmp_path / "config.yaml", config)
    assert memory.kernel_memory_limit_bytes() == expected_mb * 1024 ** 2
    caplog.set_level(logging.INFO, logger=kernels.__name__)
    limit = expected_mb * 1024 ** 2
    # Equality is allowed, and state is kept. No real large allocation is involved.
    monkeypatch.setattr(memory, "process_memory_bytes", lambda proc: limit)
    below = call("kept = 42; print(kept)")
    original = next(iter(kernels._KERNELS.values()))
    assert below["status"] == "success", below
    assert original.alive()
    assert call("print(kept)")["output"].strip() == "42"
    assert not any('"reason": "memory_ceiling"' in r.message for r in caplog.records)

    monkeypatch.setattr(memory, "process_memory_bytes", lambda proc: limit + 1)
    over = call("import time; time.sleep(3)")
    assert over["status"] == "error", over
    assert "kernel exceeded memory ceiling" in over["error"]
    assert "kernel retired" in over["error"]
    assert over["kernel"]["state_lost"] is True
    assert over["kernel"]["ended"] is True
    assert over["kernel"]["memory_bytes"] == limit + 1
    assert over["kernel"]["memory_ceiling_bytes"] == limit
    assert not kernels._KERNELS
    assert original.proc is not None and original.proc.poll() is not None
    records = [json.loads(r.message.split("kernel_lifecycle ", 1)[1])
               for r in caplog.records if r.message.startswith("kernel_lifecycle ")]
    retired = [r for r in records if r["event"] == "retire"]
    assert len(retired) == 1
    assert retired[0]["reason"] == "memory_ceiling"
    assert retired[0]["tree_memory_bytes"] == limit + 1
    assert retired[0]["memory_ceiling_bytes"] == limit
    monkeypatch.setattr(memory, "process_memory_bytes", lambda proc: 1)
    fresh = call("print('kept' in globals())")
    assert fresh["status"] == "success", fresh
    assert fresh["kernel"]["reused"] is False
    assert fresh["output"].strip() == "False"


def test_real_process_tree_memory_is_bounded(monkeypatch, tmp_path):
    from hermes_cli.config import atomic_config_write
    from tools import code_kernel_memory as memory

    # Isolate the aggregate watchdog: Linux's address-space limit can refuse the
    # parent's allocation first (thread stacks count toward AS but not RSS).
    monkeypatch.setattr(kernels, "KERNEL_RUNNER_SOURCE",
                        kernels.KERNEL_RUNNER_SOURCE.replace(memory.RUNNER_MEMORY_SOURCE, ""))
    atomic_config_write(tmp_path / "config.yaml", {
        "code_execution": {"kernel_memory_limit_mb": 200},
    })
    # Two bounded 128 MiB allocations must trip the process-tree watchdog.
    pidfile = tmp_path / "descendant.pid"
    child_code = (
        "import os, time; from pathlib import Path; blob = bytearray(128 * 1024**2); "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(8)"
    )
    code = (
        "import subprocess, sys, time\n"
        "blob = bytearray(128 * 1024**2)\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(8)\n"
    )
    started = time.monotonic()
    result = call(code)
    assert result["status"] == "error", result
    assert "kernel exceeded memory ceiling" in result["error"], result
    assert "kernel retired" in result["error"], result
    assert result["kernel"]["state_lost"] is True
    assert result["kernel"]["ended"] is True
    assert result["kernel"]["memory_bytes"] > 200 * 1024**2
    assert time.monotonic() - started < 8
    assert not kernels._KERNELS
    assert pidfile.exists(), result
    pid = int(pidfile.read_text())
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        process = None
    assert process is None or process.status() == psutil.STATUS_ZOMBIE
    # Exercise the real native/fallback sampler on this test process too.
    assert memory.process_memory_bytes(psutil.Process()) > 0
    if sys.platform == "darwin":
        assert memory.darwin_physical_footprint(psutil.Process().pid) > 0
    fresh = call("print('host alive')")
    assert fresh["status"] == "success", fresh
    assert fresh["output"].strip() == "host alive"


def test_real_allocation_memory_is_bounded(tmp_path):
    from hermes_cli.config import atomic_config_write

    atomic_config_write(tmp_path / "config.yaml", {
        "code_execution": {"kernel_memory_limit_mb": 200},
    })
    started = time.monotonic()
    result = call("kept = 42; blob = bytearray(256 * 1024**2)\nimport time; time.sleep(8)")
    assert result["status"] == "error", result
    assert "kernel exceeded memory ceiling 200 MiB" in result["error"], result
    assert time.monotonic() - started < 8
    if sys.platform.startswith("linux"):
        assert "allocation refused by OS limit" in result["error"], result
        assert "kernel kept alive" in result["error"], result
        assert not result["kernel"].get("state_lost", False)
        assert not result["kernel"].get("ended", False)
        assert next(iter(kernels._KERNELS.values())).alive()
        recovery = call("print(kept)")
        assert recovery["kernel"]["reused"] is True
        assert recovery["output"].strip() == "42"
    else:
        assert "kernel retired" in result["error"], result
        assert result["kernel"]["state_lost"] is True
        assert result["kernel"]["ended"] is True
        assert result["kernel"]["memory_bytes"] > 200 * 1024**2
        assert not kernels._KERNELS
        recovery = call("print('kept' in globals())")
        assert recovery["kernel"]["reused"] is False
        assert recovery["output"].strip() == "False"
    assert recovery["status"] == "success", recovery


@pytest.mark.parametrize("platform,rlimit_available", [
    ("linux", True), ("linux", False), ("darwin", True),
])
def test_memory_error_under_rlimit_reports_ceiling(monkeypatch, platform, rlimit_available):
    import contextlib
    import io
    import traceback
    from tools.code_kernel_memory import RUNNER_MEMORY_SOURCE

    def setrlimit(kind, limits):
        if not rlimit_available:
            raise OSError("rlimit unavailable")

    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        RLIMIT_AS=1, RLIMIT_DATA=2, RLIM_INFINITY=-1,
        getrlimit=lambda kind: (-1, -1), setrlimit=setrlimit,
    ))
    runner = {
        "sys": SimpleNamespace(platform=platform, argv=["runner.py", str(200 * 1024**2)],
                               stderr=io.StringIO()),
        "io": io, "contextlib": contextlib, "traceback": traceback, "_CAPTURE_LIMIT": 1000000,
    }
    # Execute the real runner handlers with fake OS limits, never lower this
    # pytest process's limits. Raising MemoryError makes the failure deterministic.
    exec(RUNNER_MEMORY_SOURCE + kernels.RUNNER_CELL_SOURCE, runner)
    payload, _ = runner["run_cell"]({"code": "kept = 42; raise MemoryError('refused')"}, 1)
    kernel = kernels.SessionKernel(("unit-memory-test",))
    result = kernels._cell_result(
        kernel, kernel.key, "success", payload, timeout=10, sandbox_tools=frozenset(),
        reused=False, state_reset=False, state_reset_note=None, exec_start=time.monotonic(),
    )
    assert result["status"] == "error", result
    assert "MemoryError: refused" in result["error"], result
    if platform == "linux" and rlimit_available:
        message = "kernel exceeded memory ceiling 200 MiB (allocation refused by OS limit)"
        assert message in result["error"], result
        assert message in result["output"], result
        assert "kernel kept alive" in result["error"], result
    else:
        assert "kernel exceeded memory ceiling" not in result["error"], result
    assert not kernel.closed
    assert not result["kernel"].get("state_lost", False)
    recovery, _ = runner["run_cell"]({"code": "print(kept)"}, 2)
    assert recovery["status"] == "ok"
    assert recovery["stdout"].strip() == "42"


@pytest.mark.parametrize("platform,inherited,expected", [
    ("linux", (-1, -1), (200, 200)),
    ("linux", (100, 150), (100, 150)),
    ("darwin", (-1, -1), None),
    ("win32", (-1, -1), None),
])
def test_child_rlimit_backstop_is_linux_only_and_never_widens_limits(monkeypatch, platform,
                                                                   inherited, expected):
    from tools.code_kernel_memory import RUNNER_MEMORY_SOURCE

    calls = []
    resource = SimpleNamespace(
        RLIMIT_AS=1, RLIMIT_DATA=2, RLIM_INFINITY=-1,
        getrlimit=lambda kind: inherited,
        setrlimit=lambda kind, limits: calls.append((kind, limits)),
    )
    monkeypatch.setitem(sys.modules, "resource", resource)
    child_sys = SimpleNamespace(platform=platform, argv=["runner.py", "200"], stderr=sys.stderr)
    exec(RUNNER_MEMORY_SOURCE, {"sys": child_sys})
    assert calls == ([(1, expected), (2, expected)] if expected is not None else [])
