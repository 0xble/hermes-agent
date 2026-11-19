"""Explicit Camofox tab closure through real registry dispatch and HTTP readback."""

from unittest.mock import patch

import pytest
import requests

from tests.tools.test_browser_camofox_tab_reuse import camofox as base_camofox, _dispatch


@pytest.fixture(name="camofox")
def _camofox_fixture(base_camofox):  # health: allow F811 -- pytest injects the imported camofox fixture
    return base_camofox


def _open(c, task="owner", account=None):
    args = {"url": "https://example.com"}
    if account:
        args["account"] = account
    assert _dispatch("browser_navigate", args, task)["success"]
    return c.cf._sessions[task]["tab_id"]


@pytest.mark.parametrize("explicit", [False, True])
def test_close_only_requested_tab_and_read_back(camofox, explicit):
    c = camofox
    root = _open(c)
    session = c.cf._sessions["owner"]
    session["tab_id"] = None
    session["adopt_existing_tab"] = False
    second = c.cf._ensure_tab("owner", "https://example.com/second")["tab_id"]
    assert _dispatch("browser_tabs", {"tab_id": second}, "owner")["success"]
    target = root if explicit else second
    c.server.calls.clear()

    result = _dispatch("browser_close", {"tab_id": target} if explicit else {}, "owner")

    assert result.get("success") is True, result
    assert result["closed"] is True and result["tab_id"] == target
    assert [(method, path) for method, path, _ in c.server.calls] == [
        ("DELETE", f"/tabs/{target}"), ("GET", "/tabs")]
    assert {t["tabId"] for t in c.server.tabs[session["user_id"]]} == (
        {second} if explicit else {root})
    assert session["tab_id"] == (second if explicit else None)
    assert target not in c.cf._session_tab_ids(session)
    assert not any(key[3] == target for key in c.cf._owned_tabs)
    if not explicit:
        assert not _dispatch("browser_snapshot", {}, "owner")["success"]
        assert _dispatch("browser_tabs", {"tab_id": root}, "owner")["success"]
        assert _dispatch("browser_snapshot", {}, "owner")["success"]


@pytest.mark.parametrize("refusal", ["unbound", "sibling", "account", "unknown", "profile", "server",
                                     "adopted", "protected", "live_protection", "unreadable_protection", "lease", "shared_owner",
                                     "handoff_kept"])
def test_close_refuses_unowned_scope_without_mutation(camofox, monkeypatch, refusal, tmp_path):
    c = camofox
    target = _open(c, account="brianle" if refusal == "handoff_kept" else None)
    def sibling():
        _open(c, "sibling", "brianle" if refusal == "account" else None)
        return "sibling", {"tab_id": target}

    def handoff_kept():
        c.server.open_tab_ids.append(target)
        handoff = _dispatch("browser_handoff", {"account": "brianle"}, "owner")
        assert handoff["success"] is True and handoff["tabId"] == target, handoff
        record = next(record for key, record in c.cf._owned_tabs.items() if key[-1] == target)
        assert record.get("kept") is True
        assert record.get("session_key") != c.cf.SHARED_IDENTITY_GROUP
        assert not c.cf._handoffs_in_flight
        return "owner", {}

    def configure_profile():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "different-profile"))
        return "owner", {}

    def configure_server():
        monkeypatch.setenv("CAMOFOX_URL", c.server.url + "/different-origin")
        return "owner", {}

    def configure_adopted():
        c.cf._owned_tabs.clear()  # Merely bound, like an adopted user/shared tab.
        return "owner", {}

    def configure_protected():
        c.cf.quarantine_current_protected_tab("owner")
        return "owner", {}

    def configure_live_protection():
        monkeypatch.setattr("agent.redact.has_vault_date_components", lambda task: True)
        return "owner", {}

    def configure_unreadable_protection():
        monkeypatch.setattr(c.cf, "_protected_tabs_on_disk", lambda: None)
        return "owner", {}

    def configure_lease():
        from tools import browser_camofox_leases as leases
        monkeypatch.setattr(leases, "claim", lambda *args: False)
        return "owner", {}

    def configure_shared_owner():
        c.cf._sessions["sibling"] = dict(c.cf._sessions["owner"], task_id="sibling")
        return "owner", {}

    task, args = {
        "unbound": lambda: ("unbound", {}),
        "sibling": sibling,
        "account": sibling,
        "unknown": lambda: ("owner", {"tab_id": "unreserved"}),
        "profile": configure_profile,
        "server": configure_server,
        "adopted": configure_adopted,
        "protected": configure_protected,
        "live_protection": configure_live_protection,
        "unreadable_protection": configure_unreadable_protection,
        "lease": configure_lease,
        "shared_owner": configure_shared_owner,
        "handoff_kept": handoff_kept,
    }[refusal]()
    c.server.calls.clear()

    result = _dispatch("browser_close", args, task)

    assert result.get("success") is False, result
    assert not result.get("closed"), result
    assert not any(method == "DELETE" for method, _path, _query in c.server.calls)
    assert not c.server.calls
    assert c.cf._sessions["owner"]["tab_id"] == target
    if refusal == "handoff_kept":
        assert "shown to the user" in result["error"]
        released = _dispatch("browser_handoff", {"account": "brianle", "release": True}, "owner")
        assert released["success"] is True, released
        assert c.cf._sessions["owner"]["tab_id"] == target
        c.server.calls.clear()
        closed = _dispatch("browser_close", {}, "owner")
        assert closed.get("success") is True and closed.get("closed") is True, closed
        assert any(method == "DELETE" and target in path for method, path, _query in c.server.calls)
        assert not any(tab["tabId"] == target for tabs in c.server.tabs.values() for tab in tabs)


def test_close_refuses_tab_while_handoff_is_in_flight(camofox):
    target = _open(camofox)
    user_id = camofox.cf._sessions["owner"]["user_id"]
    camofox.cf._handoffs_in_flight[user_id] = 1
    camofox.server.calls.clear()

    result = _dispatch("browser_close", {}, "owner")

    assert result["success"] is False and result["closed"] is False, result
    assert "handoff" in result["error"]
    assert not camofox.server.calls
    assert any(key[-1] == target for key in camofox.cf._owned_tabs)
    camofox.cf._handoffs_in_flight.pop(user_id)


@pytest.mark.parametrize("failure", ["ignored_delete", "delete_error", "read_error", "malformed"])
def test_close_never_claims_unverified_absence_and_can_retry(camofox, failure):
    c = camofox
    target = _open(c)
    session = c.cf._sessions["owner"]
    request = c.cf._request

    def faulty(method, path, *args, **kwargs):
        if method == "delete" and failure in {"ignored_delete", "delete_error"}:
            if failure == "delete_error":
                raise requests.Timeout("DELETE timed out")
            response = requests.Response()
            response.status_code, response._content = 200, b'{"ok":true}'
            return response
        if method == "get" and path == "/tabs":
            if failure == "read_error":
                raise requests.ConnectionError("readback unavailable")
            if failure == "malformed":
                response = requests.Response()
                response.status_code, response._content = 200, b'{"tabs":[{}]}'
                return response
        return request(method, path, *args, **kwargs)

    with patch.object(c.cf, "_request", side_effect=faulty):
        result = _dispatch("browser_close", {}, "owner")
    assert result.get("success") is False, result
    assert not result.get("closed"), result
    assert session["tab_id"] == target
    assert any(key[3] == target for key in c.cf._owned_tabs)
    assert not c.cf._closing_tabs
    retried = _dispatch("browser_close", {}, "owner")
    assert retried.get("success") is True and retried["closed"], retried


def test_delete_timeout_after_effect_is_verified(camofox):
    c = camofox
    target = _open(c)
    request = c.cf._request

    def timeout_after_delete(method, path, *args, **kwargs):
        response = request(method, path, *args, **kwargs)
        if method == "delete":
            raise requests.Timeout("response lost after deletion")
        return response

    with patch.object(c.cf, "_request", side_effect=timeout_after_delete):
        result = _dispatch("browser_close", {}, "owner")
    assert result.get("success") is True, result
    assert result["closed"] is True and result["tab_id"] == target
    assert c.cf._sessions["owner"]["tab_id"] is None


def test_close_does_not_expose_another_task_to_inflight_delete(camofox):
    c = camofox
    target = _open(c)
    request = c.cf._request

    def during_delete(method, path, *args, **kwargs):
        if method == "delete":
            assert not _dispatch("browser_snapshot", {}, "owner")["success"]
            assert _open(c, "sibling") != target
            assert not _dispatch("browser_close", {"tab_id": target}, "sibling")["success"]
        return request(method, path, *args, **kwargs)

    with patch.object(c.cf, "_request", side_effect=during_delete):
        result = _dispatch("browser_close", {}, "owner")
    assert result.get("success") is True and result["closed"], result


def test_close_pins_server_and_disables_redirects(camofox, monkeypatch):
    c = camofox
    _open(c)
    request = c.cf._request
    seen = []

    def change_server(method, path, *args, **kwargs):
        seen.append((method, kwargs["server"], kwargs["allow_redirects"]))
        response = request(method, path, *args, **kwargs)
        if method == "delete":
            monkeypatch.setenv("CAMOFOX_URL", "https://other-server.invalid")
        return response

    with patch.object(c.cf, "_request", side_effect=change_server):
        result = _dispatch("browser_close", {}, "owner")
    assert result.get("success") is True and result["closed"], result
    assert seen == [("delete", c.server.url, False), ("get", c.server.url, False)]


def test_close_schema_toolset_and_backend_gate(camofox, monkeypatch):
    from toolsets import _HERMES_CORE_TOOLS
    from tools.registry import registry

    assert "browser_close" in _HERMES_CORE_TOOLS
    entry = registry.get_entry("browser_close")
    assert entry is not None and entry.check_fn is not None
    assert entry.check_fn()
    assert entry.schema["parameters"]["required"] == []
    assert set(entry.schema["parameters"]["properties"]) == {"tab_id"}
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    assert not entry.check_fn()
    camofox.server.calls.clear()
    result = _dispatch("browser_close", {}, "owner")
    assert result.get("success") is False and not result["closed"], result
    assert not camofox.server.calls
