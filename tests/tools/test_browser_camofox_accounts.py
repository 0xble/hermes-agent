"""Named Camofox account routing and task identity binding."""

import hashlib
import json
import re
import uuid
from unittest.mock import MagicMock, patch

import pytest


def _response(data=None):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = data or {"tabId": "tab-1", "url": "https://example.com"}
    response.raise_for_status.return_value = None
    return response


@pytest.fixture(autouse=True)
def _clear_sessions():
    import tools.browser_camofox as camofox

    with camofox._sessions_lock:
        camofox._sessions.clear()
    yield
    with camofox._sessions_lock:
        camofox._sessions.clear()


def test_account_identities_are_stable_and_isolated(tmp_path, monkeypatch):
    from tools.browser_camofox_state import get_camofox_account_identity
    import tools.browser_camofox_state as state

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch.object(state, "get_hermes_home", return_value=tmp_path):
        brian = get_camofox_account_identity("brianle", "task-1")
        brian_again = get_camofox_account_identity("brianle", "task-1")
        lpg = get_camofox_account_identity("lpg", "task-1")
    assert brian == brian_again
    assert re.fullmatch(r"hermes_camofox_[0-9a-f]{24}", brian["user_id"])
    assert brian["user_id"] == "hermes_camofox_" + hashlib.sha256(
        f"camofox-account:{tmp_path / 'browser_auth' / 'camofox'}:brianle".encode()
    ).hexdigest()[:24]
    assert brian["user_id"] != lpg["user_id"]
    assert brian["session_key"] != lpg["session_key"]
    assert brian["session_key"] == "brianle_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"camofox-account-session:{tmp_path / 'browser_auth' / 'camofox'}:brianle:task-1"
    ).hex[:16]


def test_account_identity_is_scoped_to_hermes_home(tmp_path, monkeypatch):
    from tools.browser_camofox_state import get_camofox_account_identity
    import tools.browser_camofox_state as state

    with patch.object(state, "get_hermes_home", return_value=tmp_path / "one"):
        first = get_camofox_account_identity("brianle", "task")
    with patch.object(state, "get_hermes_home", return_value=tmp_path / "two"):
        second = get_camofox_account_identity("brianle", "task")
    assert first["user_id"] != second["user_id"]
    assert re.fullmatch(r"hermes_camofox_[0-9a-f]{24}", second["user_id"])


def test_aliases_select_distinct_sessions_and_echo_only_alias(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_navigate, _get_session

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    with patch("tools.browser_camofox.requests.post", return_value=_response()) as post:
        brian = json.loads(camofox_navigate("https://example.com", task_id="t-brian", account="brianle"))
        lpg = json.loads(camofox_navigate("https://example.com", task_id="t-lpg", account="lpg"))
    assert brian["account"] == "brianle"
    assert lpg["account"] == "lpg"
    assert _get_session("t-brian")["user_id"] != _get_session("t-lpg")["user_id"]
    # The raw identity is sent to Camofox but never returned to the model.
    assert "user_id" not in brian
    bodies = [call.kwargs["json"] for call in post.call_args_list if "userId" in call.kwargs["json"]]
    assert bodies[0]["userId"] == bodies[1]["userId"]
    assert bodies[2]["userId"] == bodies[3]["userId"]
    assert bodies[0]["userId"] != bodies[2]["userId"]


def test_navigation_title_lookup_uses_bound_named_account_and_exact_tab(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    seen = []

    def post(url, **kwargs):
        if url.endswith("/tabs"):
            tab = "tab-brian" if not seen else "tab-lpg"
            seen.append(kwargs["json"]["userId"])
            return _response({"tabId": tab, "url": "https://example.com"})
        return _response({"ok": True, "url": "https://example.com"})

    def get(url, **kwargs):
        user_id = kwargs["params"]["userId"]
        assert url.endswith("/tabs")
        title = "Brian title" if user_id == seen[0] else "LPG title"
        tab = "tab-brian" if user_id == seen[0] else "tab-lpg"
        return _response({"tabs": [{"tabId": "unrelated", "title": "Wrong title"},
                                   {"tabId": tab, "title": title}]})

    with (patch("tools.browser_camofox.requests.post", side_effect=post),
          patch("tools.browser_camofox.requests.get", side_effect=get) as lookup,
          patch("tools.browser_camofox.get_vnc_url", return_value=None),
          patch("tools.browser_camofox._fetch_snapshot", return_value=("", 0))):
        brian = json.loads(camofox_navigate("https://example.com", task_id="named-a", account="brianle"))
        lpg = json.loads(camofox_navigate("https://example.com", task_id="named-b", account="lpg"))

    assert brian["title"] == "Brian title"
    assert lpg["title"] == "LPG title"
    assert seen[0] != seen[1]
    assert [call.kwargs["params"]["userId"] for call in lookup.call_args_list] == seen


def test_unknown_alias_is_refused_without_http(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    with patch("tools.browser_camofox.requests.post") as post:
        result = json.loads(camofox_navigate("https://example.com", task_id="bad", account="personal"))
    assert result["success"] is False
    assert "brianle" in result["error"]
    assert "personal" in result["error"]
    post.assert_not_called()


def test_account_cannot_change_after_task_binding(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    with patch("tools.browser_camofox.requests.post", return_value=_response()):
        first = json.loads(camofox_navigate("https://example.com", task_id="same", account="brianle"))
        second = json.loads(camofox_navigate("https://example.com", task_id="same", account="meridian"))
    assert first["success"] is True
    assert second["success"] is False
    assert "already bound" in second["error"]


def test_named_account_close_drops_local_handle_without_deleting_profile(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_close, camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    with (
        patch("tools.browser_camofox.requests.post", return_value=_response()),
        patch("tools.browser_camofox.requests.delete") as delete,
    ):
        camofox_navigate("https://example.com", task_id="persistent", account="brianle")
        result = json.loads(camofox_close(task_id="persistent"))
    assert result == {"success": True, "closed": True}
    delete.assert_not_called()


def test_account_schema_is_gated_to_camofox(monkeypatch):
    import tools.browser_tool as browser_tool

    with patch.object(browser_tool, "_is_camofox_mode", return_value=False):
        assert "account" not in browser_tool._camofox_account_schema_override().get("parameters", {}).get("properties", {})
    with patch.object(browser_tool, "_is_camofox_mode", return_value=True):
        schema = browser_tool._camofox_account_schema_override()
    assert schema["parameters"]["properties"]["account"]["enum"] == ["brianle", "lpg", "meridian"]


def test_company_profile_narrows_advertised_aliases(tmp_path, monkeypatch):
    """A shared company agent must not advertise another owner's alias.

    ``browser.camofox.accounts`` scopes the operator aliases per profile; a Meridian host
    offering ``brianle`` would let one turn open a session under the wrong identity.
    """
    import tools.browser_camofox_state as state
    import tools.browser_tool as browser_tool
    from tools.browser_camofox import _resolve_account

    monkeypatch.setattr(state, "load_config", None, raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"browser": {"camofox": {"accounts": ["meridian"]}}},
    )
    assert state.get_camofox_account_aliases() == ("meridian",)
    assert _resolve_account("meridian") == "meridian"
    with pytest.raises(ValueError) as exc:
        _resolve_account("brianle")
    assert "meridian" in str(exc.value)
    with patch.object(browser_tool, "_is_camofox_mode", return_value=True):
        schema = browser_tool._camofox_account_schema_override()
    assert schema["parameters"]["properties"]["account"]["enum"] == ["meridian"]


def test_config_cannot_reintroduce_the_legacy_personal_alias(monkeypatch):
    """The replacement is a hard cutover from ``personal`` to ``brianle``, with no fallback."""
    import tools.browser_camofox_state as state

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"browser": {"camofox": {"accounts": ["personal", "brianle"]}}},
    )
    assert state.get_camofox_account_aliases() == ("brianle",)


@pytest.mark.parametrize("bad", [[], ["  "], "brianle", None, [123]])
def test_malformed_alias_config_falls_back_to_defaults(monkeypatch, bad):
    """A profile is never left with no browser identity by a malformed list."""
    import tools.browser_camofox_state as state

    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda *a, **k: {"browser": {"camofox": {"accounts": bad}}})
    assert state.get_camofox_account_aliases() == state.CAMOFOX_ACCOUNT_ALIASES


def test_configured_aliases_keep_distinct_stable_identities(tmp_path, monkeypatch):
    """Narrowing the alias list must not change an existing alias's derived identity."""
    import tools.browser_camofox_state as state

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch.object(state, "get_hermes_home", return_value=tmp_path):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {})
        default_identity = state.get_camofox_account_identity("meridian", "t1")
        monkeypatch.setattr("hermes_cli.config.load_config",
                            lambda *a, **k: {"browser": {"camofox": {"accounts": ["meridian"]}}})
        narrowed_identity = state.get_camofox_account_identity("meridian", "t1")
    assert default_identity == narrowed_identity


def test_handoff_adopts_tab_for_followup_tools(tmp_path, monkeypatch):
    import tools.browser_tool as browser_tool
    from tools import browser_camofox as camofox
    from tools.registry import registry
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(camofox, "get_vnc_url", lambda: None)
    seen = []

    def request(url, **kw):
        seen.append((url, kw))
        if url.endswith("/open"):
            return _response({"ok": True, "focused": True, "tabId": "visible-tab", "restarted": True})
        if url.endswith("/navigate"):
            return _response({"url": "https://example.com", "title": "Example"})
        if url.endswith("/click"):
            return _response({"url": "https://example.com"})
        raise AssertionError(url)

    with patch("tools.browser_camofox.requests.post", side_effect=request), patch(
            "tools.browser_camofox.requests.get", return_value=_response({"snapshot": "- button [e1]", "refsCount": 1})) as get:
        # Exercise the registered handler, not just the backend function.
        raw_handoff = registry.dispatch("browser_handoff", {"account": "brianle"}, task_id="t")
        handoff = json.loads(raw_handoff) if isinstance(raw_handoff, str) else raw_handoff
        assert handoff == {
            "success": True,
            "account": "brianle",
            "focused": True,
            "tabId": "visible-tab",
            "restarted": True,
        }
        assert json.loads(browser_tool.browser_snapshot(task_id="t"))["success"] is True
        assert json.loads(browser_tool.browser_click("@e1", task_id="t"))["success"] is True
        assert json.loads(browser_tool.browser_navigate("https://example.com", task_id="t"))["success"] is True
    assert seen[0][0].endswith(f"/browser/identities/{camofox._get_session('t')['user_id']}/open")
    assert seen[0][1]["headers"] == camofox._auth_headers()
    assert seen[0][1]["timeout"] == max(camofox._get_command_timeout(), 90)
    assert seen[1][0].endswith("/tabs/visible-tab/click")
    assert seen[2][0].endswith("/tabs/visible-tab/navigate")
    assert get.call_args_list[0].args[0].endswith("/tabs/visible-tab/snapshot")
    assert camofox._get_session("t")["user_id"] not in json.dumps(handoff)
    assert camofox._get_session("t")["tab_id"] == "visible-tab"


def test_handoff_reports_replaced_prior_tab_and_preserves_restart_flag(tmp_path, monkeypatch):
    from tools import browser_camofox as camofox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    session = camofox._get_session("replace", "brianle")
    session["tab_id"] = "old-tab"
    with patch("tools.browser_camofox.requests.post", return_value=_response({
        "ok": True, "focused": True, "tabId": "new-tab", "restarted": False,
    })):
        result = json.loads(camofox.camofox_handoff("brianle", "replace"))
    assert result["replacedTab"] is True
    assert result["restarted"] is False
    assert session["tab_id"] == "new-tab"


def test_handoff_busy_identity_leaves_tab_unchanged(tmp_path, monkeypatch):
    import requests
    from tools import browser_camofox as camofox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    session = camofox._get_session("busy", "brianle")
    session["tab_id"] = "existing-tab"
    response = _response({"error": "identity busy"})
    response.status_code = 409
    response.raise_for_status.side_effect = requests.HTTPError("409 identity busy", response=response)
    with patch("tools.browser_camofox.requests.post", return_value=response):
        result = json.loads(camofox.camofox_handoff("brianle", "busy"))
    assert result["success"] is False
    assert "another operation is using this account's browser" in result["error"].lower()
    assert "wait for it to finish" in result["error"].lower()
    assert session["tab_id"] == "existing-tab"


def test_handoff_not_configured_does_not_adopt_tab(tmp_path, monkeypatch):
    import requests
    from tools import browser_camofox as camofox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    response = _response({"error": "Shared identity not configured"})
    response.status_code = 404
    response.raise_for_status.side_effect = requests.HTTPError("404 shared_visible_01", response=response)
    with patch("tools.browser_camofox.requests.post", return_value=response):
        result = json.loads(camofox.camofox_handoff("brianle", "t"))
    assert result["success"] is False
    assert "shared visible identity on the Camofox server" in result["error"]
    assert "shared_visible_01" not in json.dumps(result)
    assert camofox._get_session("t")["tab_id"] is None


@pytest.mark.parametrize("released", [True, False])
def test_release_clears_stale_tab_and_preserves_account_binding(tmp_path, monkeypatch, released):
    from tools import browser_camofox as camofox, browser_tool
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(camofox, "_get_command_timeout", lambda: 120)
    session = camofox._get_session("release", "brianle")
    session["tab_id"] = "visible-tab"
    user_id = session["user_id"]
    with patch("tools.browser_camofox.requests.post", return_value=_response({
        "ok": True, "released": released, "userId": user_id,
    })) as post:
        raw = registry.dispatch("browser_handoff", {"account": "brianle", "release": True}, task_id="release")
    result = json.loads(raw) if isinstance(raw, str) else raw
    assert result == {"success": True, "account": "brianle", "released": released}
    assert user_id not in json.dumps(result)
    assert post.call_args.args[0].endswith(f"/browser/identities/{user_id}/release")
    assert post.call_args.kwargs["json"] == {}
    assert post.call_args.kwargs["timeout"] == 120
    assert session["tab_id"] is None
    assert camofox._get_session("release", "brianle") is session


def test_release_busy_leaves_tab_unchanged_and_requests_retry(tmp_path, monkeypatch):
    import requests
    from tools import browser_camofox as camofox, browser_tool
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
    session = camofox._get_session("busy-release", "brianle")
    session["tab_id"] = "visible-tab"
    response = _response({"error": "identity busy"})
    response.status_code = 409
    response.raise_for_status.side_effect = requests.HTTPError("409 identity busy", response=response)
    with patch("tools.browser_camofox.requests.post", return_value=response):
        raw = registry.dispatch("browser_handoff", {"account": "brianle", "release": True}, task_id="busy-release")
    result = json.loads(raw) if isinstance(raw, str) else raw
    assert result["success"] is False
    assert "wait for it to finish" in result["error"].lower()
    assert "retry the release" in result["error"].lower()
    assert session["user_id"] not in json.dumps(result)
    assert session["tab_id"] == "visible-tab"


def test_handoff_gated_to_camofox_and_refuses_switch(tmp_path, monkeypatch):
    from tools import browser_tool, browser_camofox as camofox
    from tools.registry import registry
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    assert "browser_handoff" in {entry.name for entry in registry.get_all_entries()}
    with patch.object(browser_tool, "_is_camofox_mode", return_value=False):
        assert json.loads(browser_tool.browser_handoff("brianle", "t"))["success"] is False
    with patch.object(browser_tool, "_is_camofox_mode", return_value=True), patch(
            "tools.browser_camofox.requests.post", return_value=_response({
                "ok": True, "focused": True, "tabId": "visible-tab"})) as post:
        first = json.loads(browser_tool.browser_handoff("brianle", "t"))
        second = json.loads(browser_tool.browser_handoff("meridian", "t"))
    assert first["success"] is True
    assert second["success"] is False
    assert "already bound" in second["error"]
    assert post.call_count == 1
    assert camofox._get_session("t")["tab_id"] == "visible-tab"
