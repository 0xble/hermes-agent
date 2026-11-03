"""Recover all pytest child threads before the per-file deadline kills its tree."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def _runner():
    spec = importlib.util.spec_from_file_location("run_tests_parallel", ROOT / "scripts/run_tests_parallel.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def _run_probe(tmp_path, monkeypatch, source):
    runner = _runner()
    # Exercise only pytest's built-in plugins, not the enclosing checkout fixtures.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setattr(runner, "_runner_scratch_root", lambda: str(tmp_path))
    probe = tmp_path / "test_hung_probe.py"
    probe.write_text(source, encoding="utf-8")
    return runner._run_one_file_once(probe, ["-q"], tmp_path, 3.0)


def test_file_cap_dumps_collection_stack_before_killing(tmp_path, monkeypatch):
    _, code, output, counts, wall = _run_probe(tmp_path, monkeypatch, '''
import threading

def stuck_during_collection():
    threading.Event().wait()

stuck_during_collection()
''')
    assert code == 124
    assert counts == {}
    assert "3s exceeded" in output
    if hasattr(signal, "SIGUSR2"):
        assert "stuck_during_collection" in output, output
        assert "test_hung_probe.py" in output
        assert "Python thread stacks before file timeout" in output
        print(output)
    else:
        assert "Python stack dump unavailable on this platform" in output
    # The dump must use the original budget, not an added diagnostic grace period.
    assert wall < 8, output


def test_file_cap_dumps_both_threads_after_pytest_cancels_its_timer(tmp_path, monkeypatch):
    _, code, output, _, _ = _run_probe(tmp_path, monkeypatch, '''
import threading

def test_first_fails():
    assert False

def stuck_worker():
    threading.Event().wait()

def test_stuck_main():
    threading.Thread(target=stuck_worker, daemon=True).start()
    threading.Event().wait()
''')
    assert code == 124
    if hasattr(signal, "SIGUSR2"):
        assert "stuck_worker" in output, output
        assert "test_stuck_main" in output, output
        assert output.count('Thread 0x') >= 1


def test_file_cap_dumps_after_test_protocol_finishes(tmp_path, monkeypatch):
    (tmp_path / "conftest.py").write_text('''
import threading

def pytest_sessionfinish(session, exitstatus):
    threading.Event().wait()
''', encoding="utf-8")
    _, code, output, _, _ = _run_probe(tmp_path, monkeypatch, 'def test_ok():\n    assert True\n')
    assert code == 124
    if hasattr(signal, "SIGUSR2"):
        assert "pytest_sessionfinish" in output, output
        assert "Python thread stacks before file timeout" in output


def test_fast_file_keeps_normal_pytest_output(tmp_path, monkeypatch):
    _, code, output, counts, _ = _run_probe(tmp_path, monkeypatch, 'def test_ok():\n    assert True\n')
    assert code == 0, output
    assert counts["passed"] == 1
    assert "Python thread stacks before file timeout" not in output


def _fake_timeout(tmp_path, monkeypatch, *, ready):
    runner = _runner()
    clock = [100.0]
    waits, signals, kills = [], [], []
    child = SimpleNamespace(pid=123, returncode=None, poll=lambda: None)

    def launch(cmd, **kwargs):
        scratch = Path(kwargs["env"]["TMPDIR"])
        if ready:
            (scratch / "timeout-stacks.ready").touch()
        return child

    def communicate(*, timeout):
        waits.append(timeout)
        if len(waits) <= 2:
            clock[0] += timeout
            raise subprocess.TimeoutExpired("pytest", timeout)
        child.returncode = -9
        return "", None

    child.communicate = communicate
    monkeypatch.setattr(runner, "_runner_scratch_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runner, "_kill_tree", lambda *args, **kwargs: kills.append(clock[0]))
    monkeypatch.setattr(runner, "_kill_detached_leftovers", lambda *args: None)
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: signals.append((pid, sig, clock[0])))
    result = runner._run_one_file_once(tmp_path / "test_probe.py", [], tmp_path, 3.0)
    return result, waits, signals, kills


def test_dump_request_does_not_move_the_original_deadline(tmp_path, monkeypatch):
    if not hasattr(signal, "SIGUSR2"):
        monkeypatch.setattr(signal, "SIGUSR2", 12, raising=False)
    result, waits, signals, kills = _fake_timeout(tmp_path, monkeypatch, ready=True)
    assert result[1] == 124
    assert waits == [2.25, 0.75, 10]
    assert signals == [(123, getattr(signal, "SIGUSR2"), 102.25)]
    assert kills[0] == 103.0
    assert result[4] == 3.0


def test_unregistered_child_is_not_sent_a_fatal_default_signal(tmp_path, monkeypatch):
    if not hasattr(signal, "SIGUSR2"):
        monkeypatch.setattr(signal, "SIGUSR2", 12, raising=False)
    result, waits, signals, kills = _fake_timeout(tmp_path, monkeypatch, ready=False)
    assert result[1] == 124
    assert signals == []
    assert kills[0] == 103.0
    assert "handler not ready" in result[2]
