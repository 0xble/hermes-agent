"""Registered typed card parts stay private when the normal origin probe fails.

Synthetic transports only; public dispatch, real redaction and scratch spill writes.
"""

import json
from pathlib import Path

import pytest

from agent.redact import clear_vault_date_components, clear_vault_redaction_values
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from model_tools import handle_function_call
from tools import browser_camofox as cf, browser_tool as bt, browser_tool_session as session

ORIGIN = "https://checkout.example"
TASK = "origin-card"
TREE = '- textbox "CVC" [e731]\n- textbox "Expiration date" [e4]'


@pytest.fixture(params=[TASK, "default"])
def origin_browser(monkeypatch, tmp_path, request):
    from tools import browser_vault_tool

    clear_vault_redaction_values()
    task = request.param
    state = {"origin": ORIGIN, "tree": TREE, "task": task}
    page = {"task_id": task, "tab_id": "fixture", "user_id": "fixture"}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(bt, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(bt, "_blocked_private_page_content", lambda *a: None)
    monkeypatch.setitem(bt._last_active_session_key, task, task)
    monkeypatch.setattr(bt, "get_browser_snapshot_threshold", lambda: 120)
    monkeypatch.setattr(browser_vault_tool, "_eval_js", lambda *a: (
        {"success": False, "error": "synthetic probe failure"} if state["origin"] == "failed-eval"
        else {"success": True, "result": state["origin"]}))
    monkeypatch.setattr(cf, "_require_tab", lambda *a: (page, None))
    monkeypatch.setattr(cf, "_snapshot_data", lambda *a: {"snapshot": state["tree"], "refsCount": 2})
    monkeypatch.setattr(cf, "_post", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **kw: {
        "success": True, "data": {"snapshot": state["tree"], "refs": {"e731": {}, "e4": {}}}})
    try:
        yield state
    finally:
        clear_vault_redaction_values()


def call(name, args, task=TASK):
    return handle_function_call(name, args, task_id=task, skip_pre_tool_call_hook=True,
                                skip_tool_request_middleware=True)


@pytest.mark.parametrize("backend", ["camofox", "agent-browser"])
@pytest.mark.parametrize("unknown", [None, "", "failed-eval"])
@pytest.mark.parametrize("surface", ["console", "cache"])
@pytest.mark.parametrize("ref,value,number", [("e731", "731", 731), ("e4", "09/2031", 2031)])
def test_origin_probe_failure_protects_dispatch_and_snapshot_cache(
        monkeypatch, tmp_path, origin_browser, backend, unknown, surface, ref, value, number):
    state = origin_browser
    task = state["task"]
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: backend == "camofox")
    assert json.loads(call("browser_type", {"ref": ref, "text": value}, task))["typed"] == "«redacted-vault-secret»"
    raw = {"success": True, "result": {"nested": [{"text": value, "number": number,
           "float": float(number)}], "ref": "e731", "ordinal": 7310, "enabled": True}}
    monkeypatch.setattr(bt, "_browser_eval", lambda *a: json.dumps(raw))
    state["origin"] = unknown
    protected = json.loads(call("browser_console", {"expression": "window.cardState"}, task))["result"]
    if surface == "console":
        assert protected["nested"] == [{"text": "«redacted-vault-secret»", "number": "«redacted-vault-secret»",
                                        "float": "«redacted-vault-secret»"}]
    assert protected["ref"] == "e731" and protected["ordinal"] == 7310 and protected["enabled"] is True
    state["tree"] = TREE + f'\n- textbox "Echo" [e9]: {value}\n' + '- paragraph "padding"\n' * 40
    snapshot = json.loads(call("browser_snapshot", {"full": False}, task))["snapshot"]
    assert "truncated" in snapshot
    files = list((get_hermes_home() / "cache/web").glob("browser-snapshot-*.txt"))
    assert files
    for file in files:
        assert file.is_relative_to(tmp_path)
        saved = Path(file).read_text()
        assert f": {value}" not in saved and "[e731]" in saved
    monkeypatch.setattr(bt, "get_browser_snapshot_threshold", lambda: 10000)
    assert f": {value}" not in json.loads(call("browser_snapshot", {"full": True}, task))["snapshot"]
    assert json.loads(call("browser_console", {"expression": "window.cardState"}, "other-task")) == raw
    token = set_hermes_home_override(tmp_path / "other-profile")
    try:
        assert json.loads(call("browser_console", {"expression": "window.cardState"}, task)) == raw
    finally:
        reset_hermes_home_override(token)
    state["origin"] = "https://other.example"
    assert f": {value}" in json.loads(call("browser_snapshot", {"full": True}, task))["snapshot"]
    assert json.loads(call("browser_console", {"expression": "window.cardState"}, task)) == raw
    state["origin"] = ORIGIN
    assert json.loads(call("browser_console", {"expression": "window.cardState"}, task))["result"] == protected
    clear_vault_date_components(task)
    state["origin"] = unknown
    assert json.loads(call("browser_console", {"expression": "window.cardState"}, task)) == raw


@pytest.mark.parametrize("origin", [ORIGIN, ""])
def test_default_tab_card_values_do_not_become_global_masks(origin):
    from agent.redact import (
        redact_registered_vault_number, redact_registered_vault_values,
        redact_sensitive_json, redact_sensitive_text, register_vault_card_component, register_vault_date_component,
    )
    from tools.browser_tool_snapshot import _redact_browser_output

    clear_vault_redaction_values()
    try:
        register_vault_card_component("cvc", "731", tab="default", origin=origin)
        # Generic log/terminal callers and preliminary browser redactors have no
        # owning-tab evidence. Their omitted scope must not mean the default tab.
        assert redact_sensitive_text("code=731", force=True) == "code=731"
        assert redact_registered_vault_values("731") == "731"
        assert redact_registered_vault_number(731) == 731
        assert redact_sensitive_json({"value": 731, "text": "731"}, force=True) == {"value": 731, "text": "731"}
        assert _redact_browser_output({"value": 731, "text": "731"}) == {"value": 731, "text": "731"}
        assert redact_sensitive_text("731", force=True, vault_tab="default") == "«redacted-vault-secret»"
        # Preserve the pre-existing birthday default-tab behavior independently.
        register_vault_date_component("bday-year", "1984", tab="default")
        assert redact_sensitive_text("1984", force=True) == "«redacted-vault-secret»"
    finally:
        clear_vault_redaction_values()
