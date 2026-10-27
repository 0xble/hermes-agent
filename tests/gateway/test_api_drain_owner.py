"""API admission reads its attached owner without cold-importing the gateway facade."""
import builtins
import sys
from types import ModuleType, SimpleNamespace

import pytest

from gateway.platforms.api_server import APIServerAdapter


_DRAIN_STATES = [({}, False), ({"_draining": True}, True), ({"_external_drain_active": True}, True)]


@pytest.mark.parametrize("flags,expected", _DRAIN_STATES)
def test_bound_drain_check_never_imports_gateway(monkeypatch, flags, expected):
    adapter = object.__new__(APIServerAdapter)
    adapter.gateway_runner = SimpleNamespace(**flags)
    original_import = builtins.__import__
    attempted = []

    def refuse_gateway_import(name, *args, **kwargs):
        if name == "gateway.run":
            attempted.append(name)
            raise AssertionError("a bound admission must not cold-import the gateway")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_gateway_import)
    assert adapter._gateway_is_draining() is expected
    assert attempted == []


@pytest.mark.parametrize("flags,expected", _DRAIN_STATES)
def test_unbound_drain_check_retains_global_runner_fallback(monkeypatch, flags, expected):
    adapter = object.__new__(APIServerAdapter)
    adapter.gateway_runner = None
    gateway = ModuleType("gateway.run")
    setattr(gateway, "_gateway_runner_ref", lambda: SimpleNamespace(**flags))
    monkeypatch.setitem(sys.modules, "gateway.run", gateway)
    assert adapter._gateway_is_draining() is expected
