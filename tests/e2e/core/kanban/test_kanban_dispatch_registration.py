"""SIGKILL at each claim/spawn/record/registration boundary, with real user work.

The first provider request is held until after an expired-claim tick. A duplicate
executes the same terminal command twice, independently of provider-call accounting.
FIFO barriers control the kill windows, and expiring the scratch claim replaces
waiting for the TTL. The randomized restart E2E remains unchanged.
"""
import os
import shlex
import signal
import sqlite3
import subprocess
import threading

import pytest

from tests.e2e.core.kanban._helpers import PY, REPO, Board, wait_until
from tests.e2e.core.kanban.test_kanban_dispatcher_restart import _TASK_RE
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall


class Work:
    def __init__(self, output):
        self.output = output
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, rec):
        messages = rec['body']['messages']
        tools = [m for m in messages if m.get('role') == 'tool']
        if len(tools) >= 2:
            return Text('card closed')
        if tools:
            tid = next(m.group(1) for x in messages if (m := _TASK_RE.search(str(x.get('content')))))
            return ToolCall('kanban_complete', {'summary': f'done {tid}'})
        self.entered.set()
        assert self.release.wait(90), 'provider work barrier was not released'
        return ToolCall('terminal', {'command': f'printf "work\\n" >> {shlex.quote(str(self.output))}'})


def _release(root, name):
    with (root / name).open('wb', buffering=0) as gate:
        gate.write(b'1')


@pytest.mark.platforms('linux')
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('window', [
    'claimed', 'spawned_unregistered', 'spawned_registered',
    'recorded_unregistered', 'recorded_registered',
])
def test_sigkill_registration_windows_execute_user_work_once(tmp_path, window):
    control = tmp_path / 'control'
    control.mkdir()
    for name in ('worker', 'dispatcher'):
        os.mkfifo(control / name)
    work = Work(tmp_path / 'effects')
    with FakeLLMServer(work) as server:
        board = Board(tmp_path, server.base_url)
        tid = board.create('registration race')
        probe = REPO / 'tests/hermes_cli/fixtures/kanban_sigkill_probe.py'
        with (tmp_path / 'dispatcher.log').open('wb') as log:
            dispatcher = subprocess.Popen([PY, str(probe), 'dispatcher', str(control), window],
                cwd=REPO, env=board.env(), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        worker_pid = None
        try:
            wait_until(lambda: (control / 'dispatcher.ready').exists() or dispatcher.poll() is not None,
                       60, 'dispatcher kill barrier')
            assert dispatcher.poll() is None, (tmp_path / 'dispatcher.log').read_text(encoding='utf-8')
            old_run = board.task(tid)['current_run_id']
            if window != 'claimed':
                wait_until(lambda: (control / 'worker.ready').exists(), 60, 'worker registration barrier')
                # The controlled submitter is deliberately absent from tasks.worker_pid in the
                # spawn/record gap. Read its PID from the barrier, never from process-name scans.
                worker_pid = int((control / 'worker.pid').read_text(encoding='utf-8'))
                board.spawned_pids.add(worker_pid)
            if window.endswith('_registered'):
                _release(control, 'worker')
                assert work.entered.wait(90), board.diag(tid)
            dispatcher.send_signal(signal.SIGKILL)
            dispatcher.wait(timeout=30)
            with sqlite3.connect(board.db_path) as conn:
                conn.execute('UPDATE tasks SET claim_expires=0 WHERE id=?', (tid,))
            board.dispatch()
            if window.endswith('_unregistered'):
                _release(control, 'worker')
            work.release.set()

            def drained():
                if board.task(tid)['status'] == 'done':
                    return True
                board.dispatch()
                return False

            wait_until(drained, 90, 'card completes after controlled SIGKILL')
            # A stale original submitter must have finished refusing its old claim before the
            # effect count is observed, or a late duplicate could evade the assertion.
            if worker_pid:
                from tests.e2e.core.kanban._helpers import pid_alive
                wait_until(lambda: not pid_alive(worker_pid), 60, 'original submitter exits')
            assert work.output.read_text(encoding='utf-8').splitlines() == ['work'], board.diag(tid)
            runs = board.runs(tid)
            assert sum(r['outcome'] == 'completed' for r in runs) == 1, board.diag(tid)
            if window in ('claimed', 'spawned_unregistered'):
                assert runs[0]['id'] == old_run and runs[0]['outcome'] == 'reclaimed', board.diag(tid)
            else:
                assert len(runs) == 1 and runs[0]['id'] == old_run, board.diag(tid)
        finally:
            work.release.set()
            if dispatcher.poll() is None:
                dispatcher.kill()
                dispatcher.wait(timeout=30)
            board.kill_workers()
