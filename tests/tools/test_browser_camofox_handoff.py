"""Legacy no-park lifecycle negotiation without widening parked releases."""

import json
from unittest.mock import Mock

import pytest
import requests


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    from tools import browser_camofox as cf

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CAMOFOX_URL", "http://camofox.test")
    session = {"account": "test", "user_id": "throwaway", "tab_id": "owned-tab"}
    monkeypatch.setattr(cf, "_get_session", lambda *_args: session)
    monkeypatch.setattr(cf, "keep_task_tabs", Mock())
    monkeypatch.setattr(cf, "_release_unused_tab_lease", Mock())
    from tools import browser_camofox_leases as leases
    monkeypatch.setattr(leases, "claim", lambda *_args: True)
    return cf, session


def _conflict(payload, status=409):
    response = Mock(status_code=status)
    response.json.return_value = payload
    return requests.HTTPError("lifecycle conflict", response=response)


@pytest.mark.parametrize("release,code,consent", [
    (False, "context_restart_required", {"tabId": "owned-tab", "allowRestart": True}),
    (True, "context_close_required", {"all": True}),
])
def test_no_park_retries_only_explicit_context_consent(handoff, monkeypatch, release, code, consent):
    cf, session = handoff
    success = {"ok": True, "released": True} if release else {
        "ok": True, "focused": True, "tabId": "owned-tab", "restarted": True}
    post = Mock(side_effect=[_conflict({"code": code}), success])
    monkeypatch.setattr(cf, "_post", post)

    result = json.loads(cf.camofox_handoff("test", "test-task", release=release))

    assert result["success"] is True
    assert "in-memory" in result["warning"]
    assert post.call_count == 2
    # Release is scoped to the caller's tab; only a typed no-park refusal widens it.
    assert post.call_args_list[0].args[1] == {"tabId": "owned-tab"}
    assert post.call_args_list[1].args[1] == consent
    assert post.call_args_list[0].kwargs == post.call_args_list[1].kwargs
    assert session["tab_id"] == "owned-tab"


@pytest.mark.parametrize("release,code,success", [
    (False, "context_restart_required", {"ok": True, "focused": True, "tabId": "owned-tab", "restarted": False}),
    (True, "context_close_required", {"ok": True, "released": False}),
])
def test_consent_retry_warns_only_when_state_was_discarded(handoff, monkeypatch, release, code, success):
    cf, _session = handoff
    monkeypatch.setattr(cf, "_post", Mock(side_effect=[_conflict({"code": code}), success]))
    result = json.loads(cf.camofox_handoff("test", "test-task", release=release))
    assert result["success"] is True
    assert "warning" not in result


@pytest.mark.parametrize("release", [False, True])
def test_parked_path_never_requests_context_destruction(handoff, monkeypatch, release):
    cf, session = handoff
    post = Mock(return_value={"ok": True, "released": True, "focused": True,
                              "tabId": "owned-tab", "restarted": False})
    monkeypatch.setattr(cf, "_post", post)
    result = json.loads(cf.camofox_handoff("test", "test-task", release=release))
    assert result["success"] is True
    assert "warning" not in result
    assert post.call_count == 1
    assert post.call_args.args[1] == {"tabId": "owned-tab"}
    assert session["tab_id"] == "owned-tab"


@pytest.mark.parametrize("release,payload,status", [
    (False, {"code": "context_close_required"}, 409),
    (True, {"code": "context_restart_required"}, 409),
    (False, {"error": "identity busy"}, 409),
    (True, {"error": "identity busy"}, 409),
    (False, ["context_restart_required"], 409),
    (True, {"code": "context_close_required"}, 500),
])
def test_other_failures_never_authorize_context_destruction(handoff, monkeypatch, release, payload, status):
    cf, session = handoff
    post = Mock(side_effect=_conflict(payload, status))
    monkeypatch.setattr(cf, "_post", post)
    result = json.loads(cf.camofox_handoff("test", "test-task", release=release))
    assert result["success"] is False
    assert post.call_count == 1
    assert session["tab_id"] == "owned-tab"


@pytest.mark.parametrize("release,code", [(False, "context_restart_required"), (True, "context_close_required")])
def test_context_consent_retry_is_bounded(handoff, monkeypatch, release, code):
    cf, _ = handoff
    post = Mock(side_effect=_conflict({"code": code}))
    monkeypatch.setattr(cf, "_post", post)
    result = json.loads(cf.camofox_handoff("test", "test-task", release=release))
    assert result["success"] is False
    assert post.call_count == 2
