"""Contracts for named Camofox identities."""

from __future__ import annotations

import json
import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize("winner", ["same", "different", "corrupt", "absent"])
def test_camofox_competing_claim_verifies_winner_and_cleans_only_own_temp(tmp_path, monkeypatch, winner):
    from tools import browser_camofox_state as state
    monkeypatch.setattr(state, "get_hermes_home", lambda: tmp_path)
    identity = dict(alias="personal", identity_key="key", user_id="user", session_key="session")
    expected = dict(backend="camofox", **identity)
    claim = state._binding_dir("race")
    before = {}

    def competing_rename(temporary, destination):
        assert destination == claim
        if winner != "absent":
            claim.mkdir()
            values = dict(expected)
            if winner == "different":
                values["identity_key"] = "other"
            if winner == "corrupt":
                values.pop("user_id")
            for name, value in values.items():
                (claim / name).write_text(value + "\n")
            before.update({p.name: p.read_bytes() for p in claim.iterdir()})
        raise OSError(errno.EACCES if winner == "absent" else errno.ENOTEMPTY, "race")

    monkeypatch.setattr(Path, "rename", competing_rename)
    if winner == "same":
        assert state.claim_camofox_binding("race", identity) == expected
    else:
        with pytest.raises(OSError if winner == "absent" else state.CamofoxIdentityError):
            state.claim_camofox_binding("race", identity)
    assert not list(claim.parent.glob(".*.tmp"))
    if winner != "absent":
        assert {p.name: p.read_bytes() for p in claim.iterdir()} == before


@pytest.fixture(autouse=True)
def clear_camofox_sessions():
    from tools import browser_camofox

    with browser_camofox._sessions_lock:
        browser_camofox._sessions.clear()
    yield
    with browser_camofox._sessions_lock:
        browser_camofox._sessions.clear()


def _identities():
    return {
        "real_profile_identities": {
            "personal": {"browser": "chrome", "source_profile": "Default"},
            "lpg": {"browser": "chrome", "source_profile": "Profile 1"},
            "meridian": {"browser": "chrome", "source_profile": "Profile 2"},
        }
    }


def _response(data):
    response = MagicMock()
    response.json.return_value = data
    response.raise_for_status = MagicMock()
    return response


def test_named_camofox_identity_is_home_scoped_and_opaque(tmp_path, monkeypatch):
    from tools.browser_camofox_state import resolve_camofox_identity

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "one"))
    with patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()):
        first = resolve_camofox_identity("personal", "task-a")
        second = resolve_camofox_identity("personal", "task-a")
        other = resolve_camofox_identity("lpg", "task-a")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "two"))
    with patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()):
        other_home = resolve_camofox_identity("personal", "task-a")

    assert first == second
    assert first["user_id"].startswith("hermes_camofox_")
    assert "personal" not in first["user_id"]
    assert first["user_id"] != other["user_id"] != other_home["user_id"]


def test_camofox_rejects_missing_identity_and_global_user_id(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_navigate
    from tools.browser_tool import _browser_navigate_handler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_USER_ID", "shared")
    monkeypatch.setattr("tools.browser_tool._is_camofox_mode", lambda: True)
    with patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()):
        missing = json.loads(_browser_navigate_handler({"url": "https://example.com"}, {"task_id": "task-a"}))
        conflicted = json.loads(camofox_navigate("https://example.com", task_id="task-b", identity="personal"))

    assert missing["success"] is False
    assert "identity" in missing["error"]
    assert conflicted["success"] is False
    assert "CAMOFOX_USER_ID" in conflicted["error"]


def test_camofox_task_identity_is_immutable_and_cleanup_is_task_scoped(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_close, camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    with (
        patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()),
        patch("tools.browser_camofox.requests.post", return_value=_response({"tabId": "tab-a"})),
        patch("tools.browser_camofox.requests.get", return_value=_response({"cookies": [], "origins": []})) as get,
        patch("tools.browser_camofox.requests.delete", return_value=_response({"ok": True})) as delete,
    ):
        first = json.loads(camofox_navigate("https://example.com", task_id="task-a", identity="personal"))
        switched = json.loads(camofox_navigate("https://example.com", task_id="task-a", identity="lpg"))
        closed = json.loads(camofox_close(task_id="task-a"))

    assert first["success"] is True
    assert switched["success"] is False
    assert "already bound" in switched["error"]
    assert closed["success"] is True
    assert "/storage_state" in get.call_args.args[0]
    assert "/tabs/tab-a" in delete.call_args.args[0]
    assert "/sessions/" not in delete.call_args.args[0]


def test_followups_rehydrate_durable_binding_without_identity(tmp_path, monkeypatch):
    from tools import browser_camofox
    from tools.browser_camofox import _get_session, camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    with (
        patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()),
        patch("tools.browser_camofox.requests.post", return_value=_response({"tabId": "tab-a"})),
        patch("tools.browser_camofox.requests.get", return_value=_response({"tabs": [{"tabId": "tab-a"}]})),
    ):
        assert json.loads(camofox_navigate("https://example.com", task_id="task-a", identity="personal"))["success"]
        with browser_camofox._sessions_lock:
            browser_camofox._sessions.clear()
        recovered = _get_session("task-a")

    assert recovered["named"] is True
    assert recovered["alias"] == "personal"


def test_camofox_rejects_existing_chrome_binding(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    claim = tmp_path / "browser-profile" / "agent-browser-bindings" / __import__("hashlib").sha256(b"task-a").hexdigest()
    claim.mkdir(parents=True)
    with patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()):
        result = json.loads(camofox_navigate("https://example.com", task_id="task-a", identity="personal"))

    assert result["success"] is False
    assert "another backend" in result["error"]


def test_named_camofox_renavigate_uses_existing_binding(tmp_path, monkeypatch):
    from tools.browser_tool import _browser_navigate_handler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.browser_tool._is_camofox_mode", lambda: True)
    with (
        patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()),
        patch("tools.browser_camofox.requests.post", return_value=_response({"tabId": "tab-a"})),
    ):
        first = json.loads(_browser_navigate_handler({"url": "https://example.com", "identity": "personal"}, {"task_id": "task-a"}))
        second = json.loads(_browser_navigate_handler({"url": "https://example.org"}, {"task_id": "task-a"}))

    assert first["success"] is True
    assert second["success"] is True


def test_named_close_refuses_tab_delete_without_checkpoint(tmp_path, monkeypatch):
    from tools.browser_camofox import camofox_close, camofox_navigate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with (
        patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()),
        patch("tools.browser_camofox.requests.post", return_value=_response({"tabId": "tab-a"})),
        patch("tools.browser_camofox.requests.get", side_effect=RuntimeError("storage export unavailable")),
        patch("tools.browser_camofox.requests.delete") as delete,
    ):
        assert json.loads(camofox_navigate("https://example.com", task_id="task-a", identity="personal"))["success"]
        closed = json.loads(camofox_close("task-a"))

    assert closed["success"] is False
    assert "safe storage checkpoint" in closed["error"]
    delete.assert_not_called()


def test_warm_named_session_revalidates_global_override(tmp_path, monkeypatch):
    from tools.browser_camofox import _get_session

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()):
        _get_session("task-a", identity="personal")
        monkeypatch.setenv("CAMOFOX_USER_ID", "forbidden-shared-profile")
        with pytest.raises(ValueError, match="CAMOFOX_USER_ID"):
            _get_session("task-a")


def test_camofox_session_cache_is_scoped_to_hermes_home(tmp_path, monkeypatch):
    from tools.browser_camofox import _get_session

    with patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "personal-home"))
        personal = _get_session("same-task", identity="personal")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "lpg-home"))
        lpg = _get_session("same-task", identity="lpg")

    assert personal["alias"] == "personal"
    assert lpg["alias"] == "lpg"
    assert personal["user_id"] != lpg["user_id"]


def test_native_camofox_followup_rejects_unbound_task(tmp_path, monkeypatch):
    from model_tools import handle_function_call

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.browser_tool._is_camofox_mode", lambda: True)
    with patch("tools.browser_camofox.camofox_snapshot") as snapshot:
        response = json.loads(handle_function_call("browser_snapshot", {}, task_id="unbound-task"))

    assert response["success"] is False
    assert "identity" in response["error"]
    snapshot.assert_not_called()


def test_camofox_adoption_filters_to_the_bound_identity(tmp_path, monkeypatch):
    from tools.browser_camofox import _get_session

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    with (
        patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()),
        patch("tools.browser_camofox._get", return_value={"tabs": [{"tabId": "wrong", "listItemId": "other"}]}),
    ):
        session = _get_session("task-a", identity="personal", adopt_existing_tab=True)

    assert session["tab_id"] is None


def test_camofox_schema_requires_identity_when_backend_selected(monkeypatch):
    from tools.browser_tool import _browser_navigate_schema_overrides

    monkeypatch.setattr("tools.browser_tool._is_camofox_mode", lambda: True)
    with patch("hermes_cli.browser_identity.read_browser_identity_config", return_value=_identities()):
        schema = _browser_navigate_schema_overrides()

    assert schema["parameters"]["properties"]["identity"]["enum"] == ["lpg", "meridian", "personal"]
    assert "identity" in schema["parameters"]["required"]


def test_close_rehydrated_tab_does_not_cache_deleted_tab(monkeypatch):
    from tools import browser_camofox as cf
    tabs = [{"tabId": "old", "listItemId": "task"}]
    binding = {"alias": "personal", "identity_key": "key", "user_id": "user", "session_key": "task"}
    monkeypatch.setattr(cf, "read_camofox_binding", lambda _: binding)
    monkeypatch.setattr(cf, "resolve_camofox_identity", lambda *_: binding)
    monkeypatch.setattr(cf, "_get_camofox_config", lambda: {})
    monkeypatch.setattr(cf, "_global_user_id_override", lambda _: False)
    monkeypatch.setattr(cf, "get_camofox_url", lambda: "http://fake")
    monkeypatch.setattr(cf, "_get", lambda path, **_: {"tabs": list(tabs)} if path == "/tabs" else {})
    monkeypatch.setattr(cf, "_delete", lambda *args: tabs.clear())
    monkeypatch.setattr(cf, "_post", lambda *args: {"tabId": "new"})
    assert json.loads(cf.camofox_close("task"))["success"]
    assert cf._ensure_tab("task")["tab_id"] == "new"
