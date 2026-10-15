"""Control polling stays bounded and follows the gateway serving the profile."""
from types import SimpleNamespace

import pytest

from cron.scheduler_review import _run_review_hop


def test_review_uses_serving_gateway_control_socket(tmp_path, monkeypatch):
    profile = tmp_path / "profiles/research"
    owner = tmp_path
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr("gateway.host_attach.host_gateway_serving", lambda name: SimpleNamespace(home=owner))
    calls = []
    def query(home, verb, **kwargs):
        calls.append((home, verb, kwargs))
        return {"token": "token"} if verb == "cron-review" else {"status": "completed", "reply": "PRESENT"}
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    assert _run_review_hop({"session_key": "key", "session_id": "sid"}, "report", None, None, 5) == "PRESENT"
    assert [c[0] for c in calls] == [owner, owner]
    assert calls[0][2]["payload"]["session_id"] == "sid"
    assert all(c[2]["timeout"] <= 5 for c in calls)


def test_failed_control_result_causes_original_fallback(monkeypatch):
    monkeypatch.setattr("gateway.host_attach.host_gateway_serving", lambda name: None)
    def query(home, verb, **kwargs):
        return {"token": "token"} if verb == "cron-review" else {"status": "failed", "error": "subject busy"}
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    with pytest.raises(RuntimeError, match="subject busy"):
        _run_review_hop({"session_key": "key", "session_id": "sid"}, "report", None, None, 5)


def test_other_tenant_default_gateway_is_not_used(tmp_path, monkeypatch):
    home = tmp_path / "tenant-b"
    owner = tmp_path / "tenant-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("gateway.host_attach.host_gateway_serving", lambda name: SimpleNamespace(home=owner))
    homes = []
    def query(control_home, verb, **kwargs):
        homes.append(control_home)
        return {"token": "token"} if verb == "cron-review" else {"status": "completed", "reply": "PRESENT"}
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    assert _run_review_hop({"session_key": "key", "session_id": "sid"}, "report", None, None, 5) == "PRESENT"
    assert homes == [home, home]
