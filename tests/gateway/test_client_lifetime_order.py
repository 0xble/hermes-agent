"""Ordinary gateway shutdown gets the full lease-loss grace before tool cleanup."""
import io
import signal
import sys
from types import SimpleNamespace
from unittest.mock import Mock


def test_lease_loss_signals_shutdown_before_bounded_registry_cleanup(monkeypatch):
    from gateway import client_lifetime

    events = []
    timer = Mock()
    monkeypatch.setattr(client_lifetime.threading, "Timer", lambda *a, **kw: timer)
    monkeypatch.setattr(client_lifetime, "os", SimpleNamespace(
        name="posix", getpid=lambda: 123, _exit=Mock(),
        kill=lambda pid, sig: events.append(("signal", sig))))
    monkeypatch.setattr(client_lifetime.signal, "getsignal", lambda sig: lambda *_: None)
    registry = SimpleNamespace(kill_all=lambda **kw: events.append(("registry", kw["source"])))
    monkeypatch.setitem(sys.modules, "tools.process_registry", SimpleNamespace(process_registry=registry))
    monkeypatch.delitem(sys.modules, "tools.environments.base", raising=False)
    client_lifetime._watch_client(io.BytesIO())
    assert events == [("signal", signal.SIGTERM), ("registry", "gateway_shutdown")]
    timer.start.assert_called_once()
    # Before a Python handler exists, default SIGTERM would exit immediately and
    # strand detached tools: reclaim the exact owned registry first in that case.
    events.clear()
    monkeypatch.setattr(client_lifetime.signal, "getsignal", lambda sig: signal.SIG_DFL)
    client_lifetime._watch_client(io.BytesIO())
    assert events == [("registry", "gateway_shutdown"), ("signal", signal.SIGTERM)]
    events.clear()
    client_lifetime.os.name = "nt"
    monkeypatch.setattr(client_lifetime.signal, "getsignal", lambda sig: lambda *_: None)
    client_lifetime._watch_client(io.BytesIO())
    assert events == [("registry", "gateway_shutdown"), ("signal", signal.SIGTERM)]
