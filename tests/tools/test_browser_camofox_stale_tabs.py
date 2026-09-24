"""Public-tool regression for server-destroyed Camofox tabs (#93249, #80276)."""
import json

import pytest
import requests

from tools import browser_camofox as cf, browser_tool as bt, browser_tool_eval_policy as policy
from tools import browser_vault_tool as vault


@pytest.fixture
def server(monkeypatch):
    cf._sessions.clear()
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(cf, "get_vnc_url", lambda: None)
    monkeypatch.setattr(policy, "_eval_ssrf_guard_active", lambda task_id: False)
    monkeypatch.setattr(cf, "_get_camofox_config", lambda: {})
    calls = []
    failures = {}
    created = [0]

    def request(method, path, timeout=None, **kwargs):
        calls.append((method, path, kwargs))
        if method == "post" and path == "/tabs":
            created[0] += 1
        failure = failures.get(path)
        if isinstance(failure, list):
            failure = failure.pop(0) if failure else None
        if isinstance(failure, Exception):
            raise failure
        response = requests.Response()
        response.status_code = (failure or {}).get("status", 200)
        response._content = json.dumps((failure or {}).get("body", {
            "tabId": f"tab-{created[0]}",
            "url": "https://example.test", "snapshot": "- heading", "refsCount": 1,
            "result": "ok",
        })).encode()
        response.url = "http://localhost:9377" + path
        response.raise_for_status()
        return response

    monkeypatch.setattr(cf, "_request", request)
    yield calls, failures
    cf._sessions.clear()


def _open():
    assert json.loads(bt.browser_navigate("https://example.test", task_id="stale"))["success"]
    return cf._get_session("stale")


@pytest.mark.parametrize("failure", [
    {"status": 410, "body": {"code": "tab_timeout", "recovery": "create_new_tab"}},
    {"status": 410, "body": {"code": "browser_restarted"}},
    {"status": 404, "body": {"error": "tab not found"}},
])
@pytest.mark.parametrize("operation", [
    lambda: bt.browser_snapshot(task_id="stale"),
    lambda: bt.browser_get_images(task_id="stale"),
    lambda: bt.browser_click("@e1", task_id="stale"),
    lambda: bt.browser_type("@e1", "secret-value", task_id="stale"),
    lambda: bt.browser_press("Enter", task_id="stale"),
    lambda: bt.browser_scroll("down", task_id="stale"),
    lambda: bt.browser_back(task_id="stale"),
    lambda: bt.browser_console(expression="2+2", task_id="stale"),
    lambda: bt.browser_vision("what?", task_id="stale"),
])
def test_page_operations_invalidate_without_replay(server, failure, operation):
    calls, failures = server
    session = _open()
    calls.clear()
    # All tab routes fail identically; the optional route has a different 404 contract.
    for path in ("snapshot", "click", "type", "press", "scroll", "back", "evaluate", "screenshot"):
        failures[f"/tabs/{session['tab_id']}/{path}"] = failure
    result = json.loads(operation())
    assert result["success"] is False
    assert "Call browser_navigate" in result["error"]
    assert session["tab_id"] is None
    assert not any(path == "/tabs" for _, path, _ in calls)
    assert len([path for _, path, _ in calls if path.startswith("/tabs/")]) == 1


def test_navigation_creation_adoption_and_bounded_recovery(server, monkeypatch):
    calls, failures = server
    session = _open()
    assert [p for _, p, _ in calls][:2] == ["/tabs", "/tabs/tab-1/navigate"]
    calls.clear()
    failures["/tabs/tab-1/navigate"] = {"status": 410, "body": {"code": "tab_timeout"}}
    result = json.loads(bt.browser_navigate("https://example.test", task_id="stale"))
    assert result["success"]
    assert [p for _, p, _ in calls][:3] == ["/tabs/tab-1/navigate", "/tabs", "/tabs/tab-2/navigate"]
    calls.clear()
    failures["/tabs/tab-2/navigate"] = {"status": 410, "body": {"code": "browser_restarted"}}
    failures["/tabs/tab-3/navigate"] = {"status": 410, "body": {"code": "tab_timeout"}}
    result = json.loads(bt.browser_navigate("https://example.test", task_id="stale"))
    assert not result["success"] and "Call browser_navigate" in result["error"]
    assert session["tab_id"] is None
    assert len([p for _, p, _ in calls if p == "/tabs"]) == 1

    cf._sessions.clear()
    calls.clear()
    monkeypatch.setattr(cf, "_get_camofox_config", lambda: {"user_id": "managed", "adopt_existing_tab": True})
    monkeypatch.setattr(cf, "get_camofox_url", lambda: "http://localhost:9377")
    # Adoption reuses the server tab, but still navigates explicitly.
    original = cf._request
    def adopted(method, path, timeout=None, **kwargs):
        if path == "/tabs":
            calls.append((method, path, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"tabs":[{"tabId":"existing"}]}'
            return response
        return original(method, path, timeout, **kwargs)
    monkeypatch.setattr(cf, "_request", adopted)
    assert json.loads(bt.browser_navigate("https://example.test", task_id="managed"))["success"]
    assert "/tabs/existing/navigate" in [p for _, p, _ in calls]
    assert not any(method == "post" and path == "/tabs" for method, path, _ in calls)


@pytest.mark.parametrize("failure,expected,cleared", [
    ({"status": 404, "body": {"error": "route not found"}}, "not supported", False),
    ({"status": 404, "body": {"code": "tab_not_found"}}, "Call browser_navigate", True),
    ({"status": 503, "body": {"code": "tab_unresponsive"}}, "503", False),
    ({"status": 500, "body": {"error": "server error"}}, "500", False),
])
def test_evaluate_error_classification(server, failure, expected, cleared):
    _, failures = server
    session = _open()
    failures[f"/tabs/{session['tab_id']}/evaluate"] = failure
    result = json.loads(bt.browser_console(expression="true", task_id="stale"))
    assert not result["success"] and expected in result["error"]
    assert (session["tab_id"] is None) is cleared


def test_transport_timeout_and_no_session(server):
    calls, failures = server
    result = json.loads(bt.browser_snapshot(task_id="stale"))
    assert "No browser session" in result["error"] and not calls
    session = _open()
    failures[f"/tabs/{session['tab_id']}/snapshot"] = requests.Timeout("read timed out")
    assert not json.loads(bt.browser_snapshot(task_id="stale"))["success"]
    assert session["tab_id"] is not None


def test_private_probe_optional_snapshot_and_vault(server, monkeypatch):
    calls, failures = server
    session = _open()
    monkeypatch.setattr(policy, "_eval_ssrf_guard_active", lambda task_id: True)
    failures[f"/tabs/{session['tab_id']}/evaluate"] = {"status": 410, "body": {"code": "browser_restarted"}}
    result = json.loads(bt.browser_click("@e1", task_id="stale"))
    assert "Call browser_navigate" in result["error"] and session["tab_id"] is None
    assert not any(path.endswith("/click") for _, path, _ in calls)
    monkeypatch.setattr(policy, "_eval_ssrf_guard_active", lambda task_id: False)
    failures.clear()
    assert json.loads(bt.browser_navigate("https://example.test", task_id="stale"))["success"]
    failures[f"/tabs/{session['tab_id']}/snapshot"] = {"status": 410, "body": {"code": "tab_timeout"}}
    assert json.loads(bt.browser_navigate("https://example.test", task_id="stale"))["success"]
    assert session["tab_id"] is None
    failures.clear()
    assert json.loads(bt.browser_navigate("https://example.test", task_id="stale"))["success"]
    monkeypatch.setattr(vault, "is_camofox_mode", lambda: True, raising=False)
    monkeypatch.setattr(cf, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(cf, "get_camofox_url", lambda: "http://localhost:9377")
    failures[f"/tabs/{session['tab_id']}/evaluate"] = {"status": 410, "body": {"code": "tab_timeout"}}
    calls.clear()
    result = vault._eval_js_secret("stale", "'VERY_SECRET'")
    assert not result["success"] and "Call browser_navigate" in result["error"]
    assert "VERY_SECRET" not in str(result) and session["tab_id"] is None
    assert len(calls) == 1
