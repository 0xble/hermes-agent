"""Exercise the real guard with inert signal sinks, even when ownership fails."""

import os
import signal
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

@pytest.fixture
def guarded_identity(monkeypatch, request):
    fixture_path = Path(__file__).with_name("conftest.py").resolve()
    conftest = next(plugin for plugin in request.config.pluginmanager.get_plugins()
                    if getattr(plugin, "__file__", None)
                    and Path(plugin.__file__).resolve() == fixture_path)
    current_pid = os.getpid()
    target_pid = current_pid + 100000
    state = {"recorded": False, "created": 10, "parents": [], "error": None}
    signals = []

    def process(pid):
        if pid == current_pid:
            child = SimpleNamespace(pid=target_pid, create_time=lambda: 10)
            return SimpleNamespace(children=lambda recursive: [child] if state["recorded"] else [])
        assert pid == target_pid
        if state["error"]:
            raise state["error"]
        return SimpleNamespace(create_time=lambda: state["created"], parents=lambda: state["parents"])

    def install(recorded):
        state["recorded"] = recorded
        # Capture spies before installing the fixture. No test can reach an OS signal.
        monkeypatch.setattr(os, "kill", lambda *args, **kwargs: signals.append(("kill", args)))
        if hasattr(os, "killpg"):
            monkeypatch.setattr(os, "killpg", lambda *args, **kwargs: signals.append(("killpg", args)))
        monkeypatch.setattr(psutil, "Process", process)
        request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: None))
        guard = conftest._live_system_guard.__wrapped__(request, monkeypatch)
        next(guard)
        return guard

    yield SimpleNamespace(state=state, signals=signals, install=install,
                          target_pid=target_pid, current_pid=current_pid)


@pytest.mark.parametrize("primitive", ["kill", pytest.param("killpg", marks=pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX only"))])
@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("error", ["missing", "denied", "unknown"])
def test_lookup_failure_never_reaches_signal(guarded_identity, primitive, recorded, error):
    case = guarded_identity
    guard = case.install(recorded)
    try:
        case.state["error"] = {"missing": psutil.NoSuchProcess(case.target_pid),
                               "denied": psutil.AccessDenied(case.target_pid),
                               "unknown": OSError("inspection failed")}[error]
        expected = ProcessLookupError if error == "missing" else RuntimeError
        with pytest.raises(expected):
            getattr(os, primitive)(case.target_pid, signal.SIGTERM)
        assert case.signals == []
        # Liveness probes retain their non-destructive OS semantics.
        getattr(os, primitive)(case.target_pid, 0)
        assert case.signals == [(primitive, (case.target_pid, 0))]
    finally:
        guard.close()


@pytest.mark.parametrize("primitive", ["kill", pytest.param("killpg", marks=pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX only"))])
@pytest.mark.parametrize("identity", ["matching_reparented", "new_descendant", "reused_foreign"])
def test_verified_process_identity_controls_cleanup(guarded_identity, primitive, identity):
    case = guarded_identity
    guard = case.install(recorded=True)
    try:
        if identity != "matching_reparented":
            case.state["created"] = 20
        if identity == "new_descendant":
            case.state["parents"] = [SimpleNamespace(pid=case.current_pid)]
        if identity == "reused_foreign":
            with pytest.raises(RuntimeError, match="live-system guard"):
                getattr(os, primitive)(case.target_pid, signal.SIGTERM)
            assert case.signals == []
        else:
            getattr(os, primitive)(case.target_pid, signal.SIGTERM)
            assert case.signals == [(primitive, (case.target_pid, signal.SIGTERM))]
    finally:
        guard.close()
