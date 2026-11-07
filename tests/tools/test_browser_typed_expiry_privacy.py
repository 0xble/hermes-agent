"""Synthetic expiry entry exercises real browser dispatch without a browser/account."""
import json

import pytest

from agent.redact import clear_vault_date_components, clear_vault_redaction_values
from model_tools import handle_function_call
from tools import browser_camofox as cf, browser_tool as bt, browser_tool_session as session

ORIGIN = "https://checkout.example"


@pytest.mark.parametrize("backend", ["camofox", "agent-browser"])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("label", ["Expiration date", "Expiration (MM/YY)", "MM/YY"])
@pytest.mark.parametrize("value", ["1229", "12 /29", "12/2029", "0129", "122029", "012029"])
@pytest.mark.parametrize("surface", ["type", "snapshot"])
def test_typed_expiry_masks_raw_and_formatted_owned_output(monkeypatch, tmp_path, backend, failure, label, value, surface):
    from tools import browser_vault_tool
    from agent.browser_output_egress import scrub_browser_result

    clear_vault_redaction_values()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setitem(bt._last_active_session_key, "expiry", "expiry")
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: backend == "camofox")
    monkeypatch.setattr(bt, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr(bt, "_blocked_private_page_content", lambda *a: None)
    monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: ORIGIN)
    monkeypatch.setattr(cf, "_require_tab", lambda *a: ({"task_id": "expiry", "tab_id": "fixture", "user_id": "fixture"}, None))
    typed = []
    month = "01" if value.startswith("01") else "12"
    year = "2029" if "2029" in value else "29"
    formatted = f"{month} / {year}"
    shortened = f"{month} / {year[-2:]}"
    def tree():
        return f'- textbox "{label}" [e1]' + (f': {formatted}\n- paragraph "echo {value}, shortened {shortened}"' if typed else '')
    monkeypatch.setattr(cf, "_snapshot_data", lambda *a: {"snapshot": tree(), "refsCount": 1})
    def post(path, body, **kw):
        typed.append(body["text"])
        if failure:
            raise RuntimeError(f'page rejected {body["text"]}')
        return {"ok": True}
    monkeypatch.setattr(cf, "_post", post)
    def run(task, command, args=None, **kw):
        if command == "snapshot":
            return {"success": True, "data": {"snapshot": tree(), "refs": {"e1": {}}}}
        assert command == "fill"
        typed.append(args[1])
        return {"success": not failure, "error": f'page rejected {args[1]}', "fallback_warning": f'echo {args[1]}'}
    monkeypatch.setattr(session, "_run_browser_command", run)
    def call(name, args):
        return handle_function_call(name, args, task_id="expiry", skip_pre_tool_call_hook=True,
                                    skip_tool_request_middleware=True)
    try:
        output = call("browser_type", {"ref": "@e1", "text": value})
        assert typed == [value]  # never change the literal backend input
        assert json.loads(output)["success"] is not failure
        if surface == "type":
            assert value not in output
        output = call("browser_snapshot", {"full": True})
        if surface == "snapshot":
            assert value not in output and formatted not in output and shortened not in output
        assert "[e1]" in json.loads(output)["snapshot"]
        # Exercise the real automatic truncated-snapshot cache before disk writes.
        monkeypatch.setattr(bt, "get_browser_snapshot_threshold", lambda: 10)
        call("browser_snapshot", {"full": False})
        files = list((tmp_path / "home/cache/web").glob("browser-snapshot-*.txt"))
        assert files
        for path in files:
            saved = path.read_text()
            assert value not in saved and formatted not in saved and shortened not in saved
        ordinary = json.dumps({"snapshot": f'- textbox "Order" [e1]: {value}, {formatted}, {shortened}'})
        assert scrub_browser_result("browser_snapshot", ordinary, "other-tab") == ordinary
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: "https://other.example")
        assert scrub_browser_result("browser_snapshot", ordinary, "expiry") == ordinary
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: ORIGIN)
        owned = scrub_browser_result("browser_snapshot", ordinary, "expiry")
        assert all(v not in owned for v in (value, formatted, shortened))
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: "")
        assert value not in scrub_browser_result("browser_snapshot", ordinary, "expiry")
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: ORIGIN)
        clear_vault_date_components("expiry")
        assert scrub_browser_result("browser_snapshot", ordinary, "expiry") == ordinary
    finally:
        clear_vault_redaction_values()


@pytest.mark.parametrize("label,value,expected", [
    ("Expiration date", "1229", True), ("Expiration (MM/YY)", "12 /29", True),
    ("MM/YY", "1229", True), ("Expiration month", "01", True),
    ("Expiration year", "2029", True), ("CVC", "731", True),
    ("Search", "1229", False), ("Order number", "1229", False),
    ("Issue date (MM/YY)", "1229", False), ("Valid from (MM/YY)", "1229", False),
    ("Expiration date", "1329", False), ("Expiration date", "0029", False),
    ("Expiration date", "122", False), ("Expiration date", "12299", False),
    ("Expiration date", "122029", True), ("Expiration date", "012029", True),
    ("Search", "122029", False), ("Order number", "012029", False),
    ("Issue date (MM/YY)", "122029", False), ("Valid from (MM/YY)", "012029", False),
    ("Expiration date", "132029", False), ("Expiration date", "002029", False),
])
def test_expiry_registration_requires_classified_field_and_valid_format(monkeypatch, label, value, expected):
    from agent.redact import has_vault_scoped_components
    from tools import browser_tool_snapshot, browser_vault_tool
    from tools.browser_tool_typing import register_typed_card_value

    clear_vault_redaction_values()
    monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task: ORIGIN)
    try:
        register_typed_card_value("@e1", value, "metadata", lambda: f'- textbox "{label}" [ref=e1]')
        assert has_vault_scoped_components("metadata") is expected
        text = f'value={value} order=112299 ref=e1229'
        masked = browser_tool_snapshot._redact_browser_output(text, vault_tab="metadata", vault_origin=ORIGIN)
        assert (f'value={value}' not in masked) is expected
        assert 'order=112299 ref=e1229' in masked
        # No short-number filter without an owned browser context.
        assert browser_tool_snapshot._redact_browser_output(text) == text
    finally:
        clear_vault_redaction_values()
