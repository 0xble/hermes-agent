import json
from unittest.mock import Mock

import pytest

from tools import browser_tool as bt
from tools import browser_tool_cloud as cloud
from tools import browser_tool_session as session
from tools import browser_use_cli


def _config(monkeypatch, *, target="http://127.0.0.1:9222", fallback="refuse", provider=None):
    values = {"target": target, "fallback": fallback, "cloud_provider": provider}
    monkeypatch.setattr(bt, "_browser_cfg", lambda key, default, parse, log_label: values.get(key, default))


def test_refuse_pins_session_before_cloud_local_or_cdp(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(cloud, "pinned_browser_target_or_raise", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(session._cdp, "_resolve_cdp_override", lambda target: target)
    provider = Mock()
    monkeypatch.setattr(cloud, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(session, "_create_local_session", lambda *args, **kwargs: pytest.fail("local fallback"))
    monkeypatch.setattr(session, "_create_cloud_session_or_fallback", lambda *args, **kwargs: pytest.fail("cloud fallback"))
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: pytest.fail("CDP alternative"))

    result = session._create_session_for_key("task", force_local=True)

    assert result["cdp_url"] == "http://127.0.0.1:9222"
    assert result["pinned_target"] == "http://127.0.0.1:9222"


def test_default_policy_keeps_local_selection(monkeypatch):
    _config(monkeypatch, target="", fallback="allow")
    local = {"session_name": "local", "features": {"local": True}}
    monkeypatch.setattr(session, "_create_local_session", lambda *args, **kwargs: local)
    monkeypatch.setattr(cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(session._cdp, "_get_cdp_override", lambda: None)

    assert session._create_session_for_key("task", force_local=False) is local


def test_refuse_browser_use_route_replaces_operator_endpoint(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(browser_use_cli, "_cloud_refuses_pinned_target", lambda: True)
    monkeypatch.setattr(cloud, "pinned_browser_target_or_raise", lambda: "http://127.0.0.1:9444")

    env = {"BU_CDP_URL": "http://another-browser:9222"}
    assert browser_use_cli._resolve_backend_cdp(env, "task") is None
    assert env == {"BU_CDP_URL": "http://127.0.0.1:9444"}


def test_refuse_invalid_target_is_structured(monkeypatch):
    _config(monkeypatch, target="/tmp/browser.sock")
    monkeypatch.setattr(browser_use_cli, "_cloud_refuses_pinned_target", lambda: True)

    error = browser_use_cli._resolve_backend_cdp({}, "task")
    assert error is not None
    payload = json.loads(error)

    assert payload["success"] is False
    assert payload["error_type"] == "browser_target_unavailable"
    assert payload["target"] == "/tmp/browser.sock"
    assert "loopback HTTP(S) proxy" in payload["reason"]
