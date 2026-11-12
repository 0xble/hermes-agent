"""Exec quick-command cancellation and timeout leave neither shell nor child running."""
import asyncio
from asyncio import base_subprocess
import contextlib
from contextlib import asynccontextmanager
import os
from pathlib import Path
import signal

import psutil
import pytest

from gateway import run_inbound_hooks
from gateway.run import GatewayRunner
from tests.waiting import async_wait_until

_TREE = 'echo $$ > shell.pid; sleep 120 & echo $! > child.pid; wait'


def _alive(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _pid(path: Path):
    text = path.read_text().strip() if path.exists() else ''
    return int(text) if text else None


@asynccontextmanager
async def _running_tree(tmp_path, monkeypatch, *, during_spawn=False):
    entered, release, communicating = asyncio.Event(), asyncio.Event(), asyncio.Event()
    transports, processes = [], []
    original_connect = base_subprocess.BaseSubprocessTransport._connect_pipes
    original_communicate = asyncio.subprocess.Process.communicate

    async def connect(transport, waiter):
        # The OS process exists, but create_subprocess_shell cannot return until this finishes.
        transports.append(transport)
        entered.set()
        if during_spawn:
            await release.wait()
        await original_connect(transport, waiter)

    async def communicate(proc, *args, **kwargs):
        communicating.set()
        return await original_communicate(proc, *args, **kwargs)

    monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, '_connect_pipes', connect)
    monkeypatch.setattr(asyncio.subprocess.Process, 'communicate', communicate)
    task = asyncio.create_task(
        GatewayRunner.__new__(GatewayRunner)._hm_run_exec_quick_command('probe', _TREE, cwd=str(tmp_path)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        child = await async_wait_until(lambda: _pid(tmp_path / 'child.pid'), timeout=10, what='the child pid')
        shell = _pid(tmp_path / 'shell.pid')
        assert shell is not None and child is not None
        assert shell == transports[0].get_pid()
        processes.extend((psutil.Process(shell), psutil.Process(child)))
        assert all(_alive(p) for p in processes)
        assert os.getpgid(shell) == os.getpgid(child) == shell
        if during_spawn:
            assert not communicating.is_set()
        else:
            await asyncio.wait_for(communicating.wait(), timeout=10)
        yield task, processes, release
    finally:
        release.set()
        # Also own failures before readiness/PID capture, not just the happy-path assertion.
        for transport in transports:
            try:
                shell = psutil.Process(transport.get_pid())
                processes.extend(shell.children(recursive=True))
                processes.append(shell)
                if _alive(shell) and os.getpgid(shell.pid) == shell.pid:
                    os.killpg(shell.pid, signal.SIGKILL)  # windows-footgun: ok — tests are POSIX-only
            except ProcessLookupError:
                pass
            except psutil.NoSuchProcess:
                pass
        for filename in ('child.pid', 'shell.pid'):
            pid = _pid(tmp_path / filename)
            if pid and all(p.pid != pid for p in processes):
                with contextlib.suppress(psutil.NoSuchProcess):
                    processes.append(psutil.Process(pid))
        for process in processes:
            if _alive(process):
                with contextlib.suppress(psutil.NoSuchProcess):
                    process.kill()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        for transport in transports:
            transport.close()
        await async_wait_until(lambda: all(not _alive(p) for p in processes), timeout=10,
                               what='fixture-owned processes to stop')


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
async def test_timed_out_exec_command_kills_its_background_child(tmp_path, monkeypatch):
    monkeypatch.setattr(run_inbound_hooks, '_EXEC_QUICK_COMMAND_TIMEOUT', 2)
    async with _running_tree(tmp_path, monkeypatch) as (task, processes, _):
        reply = await asyncio.wait_for(asyncio.shield(task), timeout=10)
        assert reply == run_inbound_hooks.t('gateway.quick_command.timed_out')
        await async_wait_until(lambda: all(not _alive(p) for p in processes), timeout=10,
                               what='the shell and its child to stop')


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
async def test_cancelled_exec_command_kills_shell_and_child(tmp_path, monkeypatch):
    async with _running_tree(tmp_path, monkeypatch) as (task, processes, _):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await async_wait_until(lambda: all(not _alive(p) for p in processes), timeout=10,
                               what='the shell and its child to stop')


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
async def test_cancelled_during_spawn_kills_shell_and_child(tmp_path, monkeypatch):
    async with _running_tree(tmp_path, monkeypatch, during_spawn=True) as (task, processes, release):
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await async_wait_until(lambda: all(not _alive(p) for p in processes), timeout=10,
                               what='the shell and its child to stop')


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_spawn_error_preserves_error_reply_or_cancellation(tmp_path, monkeypatch, cancelled):
    entered, release = asyncio.Event(), asyncio.Event()
    original_create = asyncio.create_subprocess_shell
    failures = []

    async def create(*args, **kwargs):
        entered.set()
        await release.wait()
        try:
            return await original_create(*args, **kwargs)
        except OSError as error:
            failures.append(error)
            raise

    monkeypatch.setattr(asyncio, 'create_subprocess_shell', create)
    task = asyncio.create_task(GatewayRunner.__new__(GatewayRunner)._hm_run_exec_quick_command(
        'probe', 'echo unreachable', cwd=str(tmp_path / 'missing')))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        if cancelled:
            task.cancel()
        release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
        else:
            reply = await asyncio.wait_for(task, timeout=10)
            assert reply == run_inbound_hooks.t('gateway.quick_command.error', error=failures[0])
        assert len(failures) == 1 and isinstance(failures[0], FileNotFoundError)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
