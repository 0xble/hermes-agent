"""Explicit Camofox tab closure through real registry dispatch and HTTP readback."""

from unittest.mock import patch

import pytest
import requests

from tests.tools.test_browser_camofox_tab_reuse import camofox, _dispatch


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
                                     "adopted", "protected", "live_protection", "unreadable_protection", "lease", "shared_owner"])
def test_close_refuses_unowned_scope_without_mutation(camofox, monkeypatch, refusal, tmp_path):
    c = camofox
    target = _open(c)
    task = "owner"
    args = {}
    if refusal == "unbound":
        task = "unbound"
    elif refusal in {"sibling", "account"}:
        _open(c, "sibling", "brianle" if refusal == "account" else None)
        task, args = "sibling", {"tab_id": target}
    elif refusal == "unknown":
        args = {"tab_id": "unreserved"}
    elif refusal == "profile":
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "different-profile"))
    elif refusal == "server":
        monkeypatch.setenv("CAMOFOX_URL", c.server.url + "/different-origin")
    elif refusal == "adopted":
        c.cf._owned_tabs.clear()  # Merely bound, like an adopted user/shared tab.
    elif refusal == "protected":
        c.cf.quarantine_current_protected_tab("owner")
    elif refusal == "live_protection":
        monkeypatch.setattr("agent.redact.has_vault_date_components", lambda task: True)
    elif refusal == "unreadable_protection":
        monkeypatch.setattr(c.cf, "_protected_tabs_on_disk", lambda: None)
    elif refusal == "lease":
        from tools import browser_camofox_leases as leases
        monkeypatch.setattr(leases, "claim", lambda *args: False)
    elif refusal == "shared_owner":
        c.cf._sessions["sibling"] = dict(c.cf._sessions["owner"], task_id="sibling")
    c.server.calls.clear()

    result = _dispatch("browser_close", args, task)

    assert result.get("success") is False, result
    assert not result.get("closed"), result
    assert not c.server.calls
    assert c.cf._sessions["owner"]["tab_id"] == target


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
