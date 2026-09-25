"""Invariant tests for protected date masking at the common browser result boundary."""
import json
from unittest.mock import patch

import pytest

from agent.browser_output_egress import scrub_browser_result
from agent.redact import (clear_vault_date_components, clear_vault_redaction_values,
                          has_vault_date_components, register_vault_date_component)

ORIGIN = "https://protected.example"
SECRET = "4"
MASK = "«redacted-vault-secret»"


@pytest.fixture(autouse=True)
def protected_date():
    clear_vault_redaction_values()
    register_vault_date_component("bday-month", SECRET,
                                  tab="journey", origin=ORIGIN)
    with patch("tools.browser_vault_tool._current_page_origin", return_value=ORIGIN):
        yield
    clear_vault_redaction_values()


def test_unlabelled_protected_input_value_and_pre_spill_snapshot_are_masked():
    from agent.redact import redact_registered_vault_snapshot
    register_vault_date_component("bday-day", "12",
                                  tab="journey", origin=ORIGIN)
    snapshot = '- spinbutton "Traveler": 12 [e1]\n- combobox "Traveler" [e2]\n  - option "4" [selected]'
    assert MASK in redact_registered_vault_snapshot(snapshot, tab="journey", origin=ORIGIN)
    assert "12 [e1]" not in redact_registered_vault_snapshot(snapshot, tab="journey", origin=ORIGIN)
    assert MASK in redact_registered_vault_snapshot(snapshot, tab="journey")
    register_vault_date_component("bday-year", "1990",
                                  tab="journey", origin=ORIGIN)
    textarea_first = '- textbox "Notes": harmless\n- spinbutton "Traveler": 1990'
    assert "Traveler\": 1990" not in redact_registered_vault_snapshot(textarea_first, tab="journey", origin=ORIGIN)


def test_managed_tab_cannot_be_adopted_after_protected_task_cleanup(tmp_path):
    from tools import browser_camofox
    session = {"task_id": "journey", "tab_id": "protected-tab", "user_id": "account", "session_key": "account"}
    browser_camofox._sessions["journey"] = session
    try:
        with patch.object(browser_camofox, "_protected_tabs_path", return_value=tmp_path / "quarantine"):
            browser_camofox._drop_session("journey")
            assert "protected-tab" in browser_camofox._protected_tab_ids
            assert "protected-tab" in browser_camofox._protected_tabs_on_disk()
            assert not has_vault_date_components("journey")
            browser_camofox._protected_tab_ids.clear()  # simulate process restart
            next_session = {"task_id": "other", "tab_id": None, "user_id": "account",
                            "session_key": "account", "adopt_existing_tab": True}
            with patch.object(browser_camofox, "get_camofox_url", return_value="http://camofox"), \
                 patch.object(browser_camofox, "_get", return_value={"tabs": [{"tabId": "protected-tab", "listItemId": "account"}]}):
                browser_camofox._adopt_existing_tab(next_session)
            assert next_session["tab_id"] is None
            # Handoff of the same identity returns the quarantined tab: it may be shown
            # to the user but must not become the new task's model-readable tab.
            handoff_session = browser_camofox._get_session("other-handoff", "brianle")
            with patch.object(browser_camofox, "_post", return_value={
                    "ok": True, "focused": True, "tabId": "protected-tab", "restarted": False}):
                handoff = json.loads(browser_camofox.camofox_handoff("brianle", "other-handoff"))
            assert handoff["success"] is True and handoff["modelDetached"] is True
            assert handoff_session["tab_id"] is None
    finally:
        browser_camofox._sessions.pop("journey", None)
        browser_camofox._sessions.pop("other-handoff", None)
        browser_camofox._protected_tab_ids.discard("protected-tab")


def test_protected_fill_detaches_other_tasks_already_bound_to_the_same_tab(tmp_path):
    """Two tasks can adopt one managed tab before either fills it. Once one fills a date
    there, only that task may keep the tab; the other must lose it, not read it."""
    from tools import browser_camofox
    from tools.browser_tool_vision import blocked_protected_date_pixels
    for task in ("filler", "observer"):
        browser_camofox._sessions[task] = {"task_id": task, "tab_id": "shared-tab", "user_id": "account",
                                           "session_key": "account", "account": None}
    try:
        with patch.object(browser_camofox, "_protected_tabs_path", return_value=tmp_path / "quarantine"):
            browser_camofox.quarantine_current_protected_tab("filler")
            register_vault_date_component("bday-month", "4", tab="filler")
            assert browser_camofox._sessions["observer"]["tab_id"] is None
            assert browser_camofox._sessions["filler"]["tab_id"] == "shared-tab"
            # A binding restored behind the quarantine's back is dropped on next access.
            browser_camofox._sessions["observer"]["tab_id"] = "shared-tab"
            with patch.object(browser_camofox, "get_camofox_url", return_value="http://camofox"), \
                 patch.object(browser_camofox, "_get", return_value={"tabs": [{"tabId": "shared-tab", "listItemId": "account"}]}):
                assert browser_camofox._get_session("observer")["tab_id"] is None
                assert browser_camofox._get_session("filler")["tab_id"] == "shared-tab"
            assert blocked_protected_date_pixels("filler") is not None
    finally:
        for task in ("filler", "observer"):
            browser_camofox._sessions.pop(task, None)
        browser_camofox._protected_tab_ids.discard("shared-tab")
        browser_camofox._protected_tab_owners.pop("shared-tab", None)


def test_binding_is_dropped_when_another_process_quarantined_the_tab(tmp_path):
    """The fill may happen in another Hermes process (CLI beside the gateway): this
    process has no in-memory record, only the persistent quarantine. An unreadable
    quarantine fails closed."""
    from tools import browser_camofox
    quarantine = tmp_path / "quarantine"
    browser_camofox._sessions["observer"] = {"task_id": "observer", "tab_id": "shared-tab", "user_id": "account",
                                             "session_key": "account", "account": None}
    browser_camofox._sessions["bystander"] = {"task_id": "bystander", "tab_id": "own-tab", "user_id": "account",
                                              "session_key": "account", "account": None}
    try:
        with patch.object(browser_camofox, "_protected_tabs_path", return_value=quarantine), \
             patch.object(browser_camofox, "get_camofox_url", return_value="http://camofox"), \
             patch.object(browser_camofox, "_get", return_value={"tabs": []}):
            browser_camofox._quarantine_protected_tab("shared-tab")  # as another process would
            browser_camofox._protected_tab_ids.discard("shared-tab")
            assert browser_camofox._get_session("observer")["tab_id"] is None
            assert browser_camofox._get_session("bystander")["tab_id"] == "own-tab"
            import shutil
            shutil.rmtree(quarantine)
            quarantine.write_text("corrupt", encoding="utf-8")  # unreadable as a record directory
            assert browser_camofox._get_session("bystander")["tab_id"] is None
    finally:
        for task in ("observer", "bystander"):
            browser_camofox._sessions.pop(task, None)


def _quarantine_in_child(path, tab_id, barrier):
    from unittest.mock import patch as _patch
    from tools import browser_camofox
    with _patch.object(browser_camofox, "_protected_tabs_path", return_value=path):
        barrier.wait()
        browser_camofox._quarantine_protected_tab(tab_id)


def test_concurrent_processes_never_lose_a_quarantine_record(tmp_path):
    """Two processes quarantining different tabs at the same moment must both persist."""
    import multiprocessing
    from tools import browser_camofox
    quarantine = tmp_path / "quarantine"
    tabs = [f"tab-{i}" for i in range(8)]
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(tabs))
    procs = [ctx.Process(target=_quarantine_in_child, args=(quarantine, tab, barrier)) for tab in tabs]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    with patch.object(browser_camofox, "_protected_tabs_path", return_value=quarantine):
        assert browser_camofox._protected_tabs_on_disk() == set(tabs)

def test_selected_option_redacts_every_matching_component_on_active_tab():
    snapshot = ('- combobox "Party size" [e1]\n'
                '  - option "4" [selected] [e2]\n'
                '- combobox "Traveler" [e3]\n'
                '  - option "4" [selected] [e4]')
    output = scrub_browser_result("browser_snapshot", json.dumps({"snapshot": snapshot}), "journey")
    lines = json.loads(output)["snapshot"].splitlines()
    assert all('option "4"' not in line for line in lines)


@pytest.mark.parametrize("name,result", [
    ("browser_console", {"messages": [{"text": "4"}]}),
    ("browser_cdp", {"result": {"result": {"value": 4}, "properties": [{"value": "4"}]}}),
    ("browser_exec", {"stdout": "4", "stderr": "4"}),
    ("browser_new_unenumerated_tool", {"unexpected": "4"}),
])
def test_every_arbitrary_browser_output_is_masked(name, result):
    actual = scrub_browser_result(name, json.dumps(result), "journey")
    assert SECRET not in str(json.loads(actual)).replace("404", "")
    assert MASK in actual
    assert scrub_browser_result(name, json.dumps(result), "another-task") == json.dumps(result)


def test_focusing_another_origin_keeps_protection_until_session_close():
    """The focused origin cannot prove the filled tab is gone (another tab may be
    focused while it stays open), so neither a probe nor a navigation lifts masking."""
    other_origin = "https://elsewhere.example"
    with patch("tools.browser_vault_tool._current_page_origin", return_value=other_origin):
        console = scrub_browser_result("browser_console", json.dumps({"value": 4}), "journey")
        navigated = scrub_browser_result("browser_navigate", json.dumps({
            "success": True, "url": other_origin + "/new", "snapshot": "Elsewhere 4"}), "journey")
    assert json.loads(console)["value"] == MASK
    assert "Elsewhere 4" not in navigated and MASK in navigated
    assert has_vault_date_components("journey")
    clear_vault_date_components("journey")  # browser session teardown
    assert not has_vault_date_components("journey")
    assert scrub_browser_result("browser_console", json.dumps({"value": 4}), "journey") == json.dumps({"value": 4})


def test_other_session_is_never_masked():
    assert scrub_browser_result("browser_console", json.dumps({"value": 4}), "unrelated") == json.dumps({"value": 4})


def test_model_tools_dispatcher_scrubs_before_hooks_and_after_plugin_transform():
    import model_tools
    observed = []
    with patch.object(model_tools, "_execute_tool", return_value=json.dumps({"result": "4"})), \
         patch.object(model_tools, "_emit_post_tool_call_hook", side_effect=lambda **kw: observed.append(kw["result"])), \
         patch.object(model_tools, "_apply_transform_tool_result_hook", return_value=json.dumps({"result": "4"})):
        result = model_tools.handle_function_call("browser_console", {"expression": "4"}, task_id="journey",
                                                  skip_pre_tool_call_hook=True, skip_tool_request_middleware=True)
    assert MASK in result and MASK in observed[0]


def test_model_tools_dispatcher_scrubs_browser_exception_before_hooks_and_logs(caplog):
    import model_tools
    hooks = []
    raw = f"page script failed near value={SECRET}"
    with patch.object(model_tools, "_execute_tool", side_effect=RuntimeError(raw)), \
         patch.object(model_tools, "_emit_post_tool_call_hook", side_effect=lambda **kw: hooks.append(kw)):
        result = model_tools.handle_function_call("browser_console", {"expression": "x"}, task_id="journey",
                                                  skip_pre_tool_call_hook=True, skip_tool_request_middleware=True)
    assert hooks[0]["status"] == "error"
    for surface in (result, hooks[0]["result"], hooks[0]["error_message"], caplog.text):
        assert f"value={SECRET}" not in surface
    assert MASK in hooks[0]["error_message"]
