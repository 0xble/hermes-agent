"""Caller-scoped release, truthful diagnostics and task-affine popup selection."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from tests.tools.test_browser_camofox_tab_reuse import camofox, _dispatch


def navigate(fixture, task="owner"):
    assert _dispatch("browser_navigate", {"url": "https://shop.example.test/", "account": "brianle"}, task)["success"]
    return fixture.cf._sessions[task]


def test_release_sends_callers_tab_not_last_visible(camofox):
    first = navigate(camofox)
    second = navigate(camofox, "other")
    assert first["tab_id"] != second["tab_id"]
    result = _dispatch("browser_handoff", {"account": "brianle", "release": True}, "owner")
    assert result["success"]
    method, path, body = camofox.server.calls[-1]
    assert method == "POST" and path.endswith("/release")
    assert body == {"tabId": first["tab_id"]}
    assert camofox.cf._sessions["other"]["tab_id"] == second["tab_id"]


def test_unbound_release_never_sends_unscoped_request(camofox):
    navigate(camofox, "other")
    before = len(camofox.server.calls)
    result = _dispatch("browser_handoff", {"account": "brianle", "release": True}, "owner")
    assert not result["success"]
    assert len(camofox.server.calls) == before


def test_console_capture_is_unsupported_but_evaluation_works(camofox, monkeypatch):
    session = navigate(camofox)
    result = _dispatch("browser_console", {"clear": True}, "owner")
    assert result["success"] is False and result["supported"] is False
    assert result["error_type"] == "unsupported_capability"
    assert "total_errors" not in result
    post = Mock(return_value={"result": "Popup test"})
    monkeypatch.setattr(camofox.cf, "_post", post)
    result = _dispatch("browser_console", {"expression": "document.title"}, "owner")
    assert result["success"]
    assert post.call_args.args[0] == f"/tabs/{session['tab_id']}/evaluate"


@pytest.mark.parametrize("failure", [None, "stale", "unavailable"])
def test_annotated_vision_reports_limits_and_keeps_screenshot(camofox, monkeypatch, failure):
    from agent import auxiliary_client
    session = navigate(camofox)
    image = b"synthetic png bytes"
    monkeypatch.setattr(camofox.cf, "_get_raw", lambda *a, **kw: SimpleNamespace(content=image))
    call = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Page"))]))
    monkeypatch.setattr(auxiliary_client, "call_llm", call)
    if failure:
        response = requests.Response()
        response.status_code = 410 if failure == "stale" else 500
        error = requests.HTTPError(response=response)
        monkeypatch.setattr(camofox.cf, "_snapshot_data", Mock(side_effect=error))
    result = _dispatch("browser_vision", {"question": "Describe", "annotate": True}, "owner")
    assert result["success"]
    assert result["annotation"] == {"requested": True, "supported": False, "applied": False,
                                     "error_type": "unsupported_capability", "text_context_available": failure is None}
    from pathlib import Path
    assert Path(result["screenshot_path"]).read_bytes() == image
    assert result["warnings"]
    if failure == "stale":
        assert result["interaction_error_type"] == "stale_tab"
        assert session["tab_id"] is None
    else:
        assert "interaction_error_type" not in result


def test_popup_is_discoverable_selectable_and_keeps_opener_reserved(camofox):
    session = navigate(camofox)
    opener = session["tab_id"]
    camofox.server.seed(session["user_id"],
                        {"tabId": "popup", "listItemId": session["session_key"],
                         "url": "https://oauth.example.test/", "title": "Authorize"})
    listed = _dispatch("browser_tabs", {}, "owner")
    assert {tab["tab_id"] for tab in listed["tabs"]} == {opener, "popup"}
    assert listed["selected_tab_id"] == opener
    assert _dispatch("browser_tabs", {"tab_id": "popup"}, "owner")["snapshot_required"]
    assert "popup" in _dispatch("browser_snapshot", {}, "owner")["snapshot"]
    assert not _dispatch("browser_tabs", {"tab_id": "popup"}, "other")["success"]
    assert _dispatch("browser_tabs", {"tab_id": opener}, "owner")["success"]
    assert camofox.cf._sessions["owner"]["tab_id"] == opener
    camofox.cf._get_session("other", "brianle")
    camofox.cf._adopt_existing_tab(camofox.cf._sessions["other"])
    assert camofox.cf._sessions["other"]["tab_id"] is None


def test_tabs_never_show_other_task_or_human_metadata(camofox):
    session = navigate(camofox)
    other = navigate(camofox, "other")
    camofox.server.seed(session["user_id"],
                        {"tabId": "other-popup", "listItemId": other["session_key"], "title": "private"},
                        {"tabId": "human", "listItemId": "__shared_identity__", "title": "private"})
    result = _dispatch("browser_tabs", {}, "owner")
    assert {tab["tab_id"] for tab in result["tabs"]} == {session["tab_id"]}
    assert "private" not in str(result)
    for target in [other["tab_id"], "other-popup", "human", "missing"]:
        assert not _dispatch("browser_tabs", {"tab_id": target}, "owner")["success"]


def test_shared_popups_require_explicit_opener_affinity(camofox):
    assert _dispatch("browser_handoff", {"account": "brianle"}, "owner")["success"]
    session = camofox.cf._sessions["owner"]
    camofox.server.seed(session["user_id"],
                        {"tabId": "owned-popup", "listItemId": "__shared_identity__", "openerTabId": session["tab_id"]},
                        {"tabId": "unrelated", "listItemId": "__shared_identity__"})
    assert {tab["tab_id"] for tab in _dispatch("browser_tabs", {}, "owner")["tabs"]} == {"visible-tab", "owned-popup"}
    assert _dispatch("browser_tabs", {"tab_id": "owned-popup"}, "owner")["success"]


@pytest.mark.parametrize("fence", ["protected", "quarantine", "stale", "closing", "lease"])
def test_popup_selection_obeys_existing_fences(camofox, monkeypatch, fence):
    session = navigate(camofox)
    camofox.server.seed(session["user_id"], {"tabId": "popup", "listItemId": session["session_key"]})
    if fence == "protected":
        camofox.cf._protected_tab_ids.add("popup")
    elif fence == "quarantine":
        camofox.cf._quarantine_protected_tab("popup")
    elif fence == "stale":
        camofox.cf._stale_tab_ids[session["user_id"]] = {"popup"}
    elif fence == "closing":
        camofox.cf._closing_tabs.add((camofox.server.url, session["user_id"], "popup"))
    else:
        from tools import browser_camofox_leases as leases
        original = leases.claim
        monkeypatch.setattr(leases, "claim", lambda server, user, tab: tab != "popup" and original(server, user, tab))
    assert not _dispatch("browser_tabs", {"tab_id": "popup"}, "owner")["success"]
    assert session["tab_id"] != "popup"


def test_tab_family_survives_compression_and_releases_leases(camofox):
    from tools import browser_camofox_leases as leases
    session = navigate(camofox)
    camofox.server.seed(session["user_id"], {"tabId": "popup", "listItemId": session["session_key"]})
    assert _dispatch("browser_tabs", {"tab_id": "popup"}, "owner")["success"]
    camofox.cf.carry_task_binding("owner", "next", move=True)
    assert _dispatch("browser_tabs", {"tab_id": session["tab_id"]}, "next")["success"]
    camofox.cf.release_task_bindings(["next"])
    assert not leases._handles
