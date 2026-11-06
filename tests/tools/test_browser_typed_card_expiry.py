"""Direct expiry entry uses existing checkout classification and browser egress scope."""
import json

import pytest

from agent.browser_output_egress import scrub_browser_result
from agent.redact import clear_vault_redaction_values
from tools import browser_tool as bt, browser_tool_session as session


@pytest.mark.parametrize("backend", ["camofox", "agent-browser"])
@pytest.mark.parametrize("label,value", [("Expiration month", "09"), ("Expiry year", "2031"),
                                         ("Expiration date", "09/31"), ("Valid thru", "09/2031")])
def test_expiry_typed_public_entrypoint_is_scoped_and_cleans_up(monkeypatch, backend, label, value):
    from tools import browser_camofox as cf
    clear_vault_redaction_values()
    task = "expiry-task"
    origin = {"value": "https://shop.example"}
    tree = f'- textbox "{label}" [e109]\n- textbox "Search" [e2]'
    bt._last_active_session_key[task] = task
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: backend == "camofox")
    monkeypatch.setattr(bt, "_blocked_private_page_action", lambda *a: None)
    monkeypatch.setattr("tools.browser_vault_tool._current_page_origin", lambda _: origin["value"])
    monkeypatch.setattr(session, "_run_browser_command", lambda *a, **kw:
                        {"success": True, "data": {"snapshot": tree}})
    page = {"task_id": task, "tab_id": "owned", "user_id": "fixture"}
    monkeypatch.setattr(cf, "_require_tab", lambda *a: (page, None))
    monkeypatch.setattr(cf, "_snapshot_data", lambda *a: {"snapshot": tree})
    monkeypatch.setattr(cf, "_post", lambda *a, **kw: {"ok": True})
    try:
        result = json.loads(bt.browser_type("e109", value, task_id=task))
        assert result["typed"] == "«redacted-vault-secret»"
        raw = json.dumps({"snapshot": f'- textbox "Echo" [e109]: {value}\n- item "ordinal 1090"',
                          "result": {"value": value, "number": int(value) if value.isdigit() else 0}})
        protected = scrub_browser_result("browser_console", raw, task)
        assert f": {value}" not in protected
        assert json.loads(protected)["result"]["value"] == "«redacted-vault-secret»"
        assert json.loads(protected)["result"]["number"] == (
            "«redacted-vault-secret»" if value.isdigit() else 0)
        assert "e109" in protected and "ordinal 1090" in protected
        assert scrub_browser_result("browser_console", raw, "other-task") == raw
        origin["value"] = "https://other.example"
        assert scrub_browser_result("browser_console", raw, task) == raw
        origin["value"] = "https://shop.example"
        assert json.loads(scrub_browser_result("browser_console", raw, task))["result"]["value"] == "«redacted-vault-secret»"
        from agent.redact import clear_vault_date_components
        clear_vault_date_components(task)
        assert scrub_browser_result("browser_console", raw, task) == raw
    finally:
        clear_vault_redaction_values()
        bt._last_active_session_key.pop(task, None)


@pytest.mark.parametrize("label,value", [("Search", "731"), ("One-time code", "731"),
                                         ("Issue date MM/YY", "09/31"), ("Valid from", "09/31")])
def test_unrelated_control_metadata_never_registers_card_parts(label, value, monkeypatch):
    from tools.browser_tool_typing import register_typed_card_value
    from agent.redact import has_vault_scoped_components
    clear_vault_redaction_values()
    monkeypatch.setattr("tools.browser_vault_tool._current_page_origin", lambda _: "https://shop.example")
    try:
        register_typed_card_value("e2", value, "unrelated", lambda: f'- textbox "{label}" [e2]')
        assert not has_vault_scoped_components("unrelated")
    finally:
        clear_vault_redaction_values()
