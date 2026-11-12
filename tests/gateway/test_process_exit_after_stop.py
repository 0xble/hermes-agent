"""Process exit must follow durable stop, not asyncio's worker-thread joins."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize("outcome", [0, 1, 78, "KeyboardInterrupt", "CancelledError"])
@pytest.mark.parametrize("entrypoint", ["module", "cli"])
def test_process_main_exits_after_stop_with_blocked_executor(tmp_path, outcome, entrypoint):
    exit_code = outcome if isinstance(outcome, int) else 0
    marker = tmp_path / "persisted"
    script = f'''
import asyncio, os, threading
from pathlib import Path
import gateway.run as gateway
import hermes_cli.boot_bootstrap
hermes_cli.boot_bootstrap.maybe_run_boot_bootstrap = lambda _root: None

async def fake_stop(self):
    read_fd, write_fd = os.pipe()
    threading.Thread(target=lambda: os.read(read_fd, 1), daemon=False).start()
    entered = threading.Event()
    def blocked_cleanup():
        entered.set()
        threading.Event().wait()
    asyncio.get_running_loop().run_in_executor(None, blocked_cleanup)
    await asyncio.to_thread(entered.wait)
    # Model stop()'s explicitly awaited persistence before process exit.
    await asyncio.to_thread(Path({str(marker)!r}).write_text, "durable-stop")

async def fake_gateway(*args, **kwargs):
    runner = object.__new__(gateway.GatewayRunner)
    runner._stop_task = None
    gateway.GatewayRunner._stop_impl = fake_stop
    await runner.stop()
    if {outcome == "KeyboardInterrupt"!r}:
        raise KeyboardInterrupt()
    if {outcome == "CancelledError"!r}:
        raise asyncio.CancelledError()
    if {exit_code == 78!r}:
        raise SystemExit(78)
    return {exit_code == 0!r}
gateway.start_gateway = fake_gateway
if {entrypoint == "cli"!r}:
    from tests.hermes_cli.test_gateway_run_hard_exit import _prepare
    from pytest import MonkeyPatch
    patches = MonkeyPatch()
    real_exit = gateway._exit_after_graceful_shutdown
    cli = _prepare(patches)
    gateway.start_gateway = fake_gateway
    gateway._exit_after_graceful_shutdown = real_exit
    cli._respawn_storm_backoff = lambda: None
    cli.run_gateway()
else:
    gateway.main()
'''
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=5, env=dict(os.environ))
    except subprocess.TimeoutExpired:
        pytest.fail("stop persisted, but process waited >5s for blocked worker finalization")
    elapsed = time.monotonic() - started
    print(f"process exit code={exit_code} elapsed={elapsed:.3f}s")
    assert result.returncode == exit_code, result.stderr
    assert marker.read_text() == "durable-stop"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stop_kind", ["planned-SIGTERM", "SIGINT", "unplanned-SIGTERM"])
def test_signal_handler_process_exits_after_bounded_stop(tmp_path, stop_kind):
    marker = tmp_path / "persisted"
    script = f'''
import asyncio, os, signal, threading
from pathlib import Path
from pytest import MonkeyPatch
import gateway.run as gateway
from gateway import status
from gateway.run_process_exit import run_gateway_process
from tests.gateway.test_planned_restart_signal import _deliver_sigterm
patches = MonkeyPatch()
patches.setattr(gateway, "_hermes_home", Path(os.environ["HERMES_HOME"]))
async def deliver_signal(handler, sig):
    delivered = asyncio.Event()
    def receive():
        handler(sig)
        delivered.set()
    asyncio.get_running_loop().add_signal_handler(sig, receive)
    os.kill(os.getpid(), sig)  # This disposable test child only, never a live gateway.
    await delivered.wait()
# Leave a real blocked executor worker behind after every durable stop phase.
async def fake_gateway():
    if {stop_kind == "planned-SIGTERM"!r}:
        assert status.write_planned_stop_marker(os.getpid())
    runner, sweeps, unexpected = await _deliver_sigterm(
        patches, signal.SIGINT if {stop_kind == "SIGINT"!r} else signal.SIGTERM,
        deliver_signal=deliver_signal)
    await asyncio.to_thread(Path({str(marker)!r}).write_text, "durable-stop")
    entered = threading.Event()
    def blocked():
        entered.set()
        threading.Event().wait()
    asyncio.get_running_loop().run_in_executor(None, blocked)
    await asyncio.to_thread(entered.wait)
    assert all(timeout is not None for _phase, timeout, _restart in sweeps)
    return not unexpected
asyncio.run(run_gateway_process(fake_gateway))
'''
    started = time.monotonic()
    # The existing planned-stop post-interrupt grace alone is 5s; include it
    # and interpreter imports without altering any production drain budget.
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=15, env=dict(os.environ))
    elapsed = time.monotonic() - started
    print(f"signal-handler stop={stop_kind} code={result.returncode} elapsed={elapsed:.3f}s")
    assert result.returncode == (1 if stop_kind == "unplanned-SIGTERM" else 0), result.stderr
    assert marker.read_text() == "durable-stop"


def test_local_shutdown_skips_directory_scan_but_normal_cleanup_runs(tmp_path, monkeypatch):
    import glob
    from tools.environments import local
    original_glob = glob.glob
    env = object.__new__(local.LocalEnvironment)
    env._snapshot_path = str(tmp_path / "snapshot")
    env._cwd_file = str(tmp_path / "cwd")
    stale = tmp_path / "snapshot.tmp.123"
    for path in (Path(env._snapshot_path), Path(env._cwd_file), stale):
        path.write_text("owned")
    env.cleanup()
    assert not any(path.exists() for path in (Path(env._snapshot_path), Path(env._cwd_file), stale))
    for path in (Path(env._snapshot_path), Path(env._cwd_file)):
        path.write_text("owned")
    def blocked_scan(*args, **kwargs):
        pytest.fail("process-exit cleanup must not enumerate the shared temp directory")
    monkeypatch.setattr(glob, "glob", blocked_scan)
    with local.process_exit_cleanup():
        env.cleanup()
    assert not any(path.exists() for path in (Path(env._snapshot_path), Path(env._cwd_file), stale))
    monkeypatch.setattr(glob, "glob", original_glob)
    # The scope must not leak to the next ordinary session cleanup.
    stale.write_text("owned")
    env.cleanup()
    assert not stale.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["post-interrupt", "final"])
async def test_signal_stop_bounds_cleanup_even_without_restart_intent(monkeypatch, phase):
    import asyncio
    import threading
    from gateway.run_shutdown import GatewayShutdownMixin
    from tests.gateway.test_shutdown_kill_off_loop import _make_ctx, _make_phase_runner

    runner, _ = _make_phase_runner(monkeypatch, [])
    runner._signal_initiated_shutdown = True
    runner._restart_requested = False
    runner._restart_shutdown_bound = lambda: 0.05
    entered, release = threading.Event(), threading.Event()

    def blocked_cleanup(*args, **kwargs):
        entered.set()
        release.wait(5)
        return []

    monkeypatch.setattr(GatewayShutdownMixin, "_stop_kill_tool_subprocesses", staticmethod(blocked_cleanup))
    if phase == "post-interrupt":
        call = runner._stop_interrupt_remaining_work(_make_ctx())
    else:
        call = runner._stop_release_runtime_state(_make_ctx())
    operation = asyncio.create_task(call)
    done, _ = await asyncio.wait({operation}, timeout=0.5)
    release.set()
    await operation
    assert entered.is_set()
    assert operation in done, "signal stop must bound best-effort cleanup, independently of restart intent"
