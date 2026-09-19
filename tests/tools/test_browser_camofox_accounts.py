"""Named Camofox account routing and task identity binding."""

import json
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
    assert brian["user_id"] != lpg["user_id"]
    assert brian["session_key"] != lpg["session_key"]


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
    assert bodies[0]["userId"] != bodies[1]["userId"]


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
