"""Pixel captures must not cross the protected-date browser boundary."""
import json
from unittest.mock import patch

import pytest

from agent.redact import clear_vault_date_components, register_vault_date_component
from tools import browser_tool as bt
from tools import browser_camofox
from tools.browser_cdp_tool import browser_cdp

ORIGIN = "https://protected.example"


@pytest.fixture
def filled_date():
    register_vault_date_component("bday-month", "4", tab="journey", origin=ORIGIN)
    yield
    clear_vault_date_components("journey")


@pytest.mark.parametrize("native,lightpanda", [(True, False), (False, False), (False, True)])
def test_vision_refuses_pixels_before_any_capture(filled_date, native, lightpanda):
    with patch.object(bt, "_is_camofox_mode", return_value=False), \
         patch.object(bt, "_last_session_key", side_effect=lambda task: task), \
         patch("tools.browser_vault_tool._current_page_origin", return_value=ORIGIN), \
         patch.object(bt._vision._cloud, "_get_browser_engine", return_value="lightpanda" if lightpanda else "chrome"), \
         patch.object(bt._vision, "_lightpanda_vision_preroute") as preroute, \
         patch.object(bt, "_capture_vision_screenshot") as capture, \
         patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=native):
        result = json.loads(bt.browser_vision("what is here?", task_id="journey"))
    assert result["success"] is False and "screenshot" in result["error"].lower()
    assert "browser_handoff" in result["error"]
    preroute.assert_not_called()
    capture.assert_not_called()


def test_vision_refusal_survives_focus_elsewhere_and_lifts_on_session_close(filled_date):
    with patch.object(bt, "_is_camofox_mode", return_value=False), \
         patch.object(bt, "_last_session_key", side_effect=lambda task: task), \
         patch.object(bt, "_capture_vision_screenshot", return_value=({}, None, "capture reached")) as capture, \
         patch("tools.browser_vault_tool._current_page_origin", return_value=ORIGIN):
        assert bt.browser_vision("what?", task_id="other") == "capture reached"
        assert capture.call_count == 1
    with patch.object(bt, "_is_camofox_mode", return_value=False), \
         patch.object(bt, "_last_session_key", side_effect=lambda task: task), \
         patch.object(bt, "_capture_vision_screenshot", return_value=({}, None, "capture reached")) as capture, \
         patch("tools.browser_vault_tool._current_page_origin", return_value="https://elsewhere.example"):
        refused = json.loads(bt.browser_vision("what?", task_id="journey"))
        assert refused["success"] is False and "screenshot" in refused["error"].lower()
        capture.assert_not_called()
    clear_vault_date_components("journey")  # browser session close
    with patch.object(bt, "_is_camofox_mode", return_value=False), \
         patch.object(bt, "_last_session_key", side_effect=lambda task: task), \
         patch.object(bt, "_capture_vision_screenshot", return_value=({}, None, "capture reached")) as capture:
        assert bt.browser_vision("what?", task_id="journey") == "capture reached"
        capture.assert_called_once()


def test_camofox_refuses_before_rest_screenshot(filled_date):
    session = {"task_id": "journey", "tab_id": "protected-tab", "user_id": "account"}
    with patch.object(browser_camofox, "_require_tab", return_value=(session, None)), \
         patch("tools.browser_vault_tool._current_page_origin", return_value=ORIGIN), \
         patch.object(browser_camofox, "_get_raw") as capture:
        result = json.loads(browser_camofox.camofox_vision("what?", task_id="journey"))
    assert result["success"] is False and "screenshot" in result["error"].lower()
    capture.assert_not_called()


def test_arbitrary_browser_exec_cannot_bypass_pixel_guard(filled_date):
    from tools.browser_use_cli import browser_exec
    with patch("tools.browser_vault_tool._current_page_origin", return_value=ORIGIN), \
         patch("tools.browser_use_cli._run_cli_killing_process_group") as execute:
        result = json.loads(browser_exec("print(capture_screenshot())", task_id="journey"))
    assert result["success"] is False and "screenshot" in result["error"].lower()
    execute.assert_not_called()


def test_unknown_origin_retains_pixel_refusal(filled_date):
    with patch("tools.browser_vault_tool._current_page_origin", return_value=None), \
         patch.object(bt, "_is_camofox_mode", return_value=False), \
         patch.object(bt, "_last_session_key", side_effect=lambda task: task), \
         patch.object(bt, "_capture_vision_screenshot") as capture:
        result = json.loads(bt.browser_vision("what?", task_id="journey"))
    assert result["success"] is False and "screenshot" in result["error"].lower()
    capture.assert_not_called()


@pytest.mark.parametrize("method", ["Page.captureScreenshot", "Page.printToPDF", "HeadlessExperimental.beginFrame"])
def test_cdp_pixel_methods_refused(filled_date, method):
    with patch("tools.browser_vault_tool._current_page_origin", return_value=ORIGIN), \
         patch("tools.browser_cdp_tool._resolve_cdp_endpoint", return_value="ws://localhost"), \
         patch("tools.browser_cdp_tool._run_async") as capture:
        result = json.loads(browser_cdp(method, task_id="journey"))
    assert result.get("success") is False and "screenshot" in result["error"].lower()
    capture.assert_not_called()


def test_remapped_session_key_shares_protected_state_across_fill_and_readers():
    """A task whose browser runs under a remapped session key (hybrid sidecar) must hit the
    same protected-date entry from the fill, the output egress and the pixel guard."""
    from agent.browser_output_egress import scrub_browser_result
    from agent.vault_store import VaultItemMeta
    from tools import browser_vault_tool
    from tools.browser_tool_vision import blocked_protected_date_pixels

    meta = VaultItemMeta(id="op:field:remap", kind="protected_field", label="Traveler date of birth",
                         origin=ORIGIN, created_at="", allowed_origins=(ORIGIN,), field_token="bday")

    class Backend:
        name, display_name, needs_unlock, binds_cards_to_page = "onepassword", "1Password", False, False

        def is_unlocked(self):
            return True

        def get_meta(self, handle):
            return meta if handle == meta.id else None

        def resolve_secret(self, handle):
            return {"value": "1990-04-12"}

    controls = [{"autocomplete": "bday", "formIndex": 0, "index": 0, "label": "Date of birth",
                 "name": "dateOfBirth", "type": "date"}]
    remap = {"sidecar-task": "sidecar-task::private"}
    private = {"sidecar-task::private": {
        "session_key": "sidecar-task::private", "owner_task_id": "sidecar-task",
        "session_name": "local-sidecar", "features": {"local": True},
    }}
    with patch.object(bt, "_active_sessions", private), \
         patch("tools.browser_tool_cdp._get_cdp_override", return_value=None), \
         patch("tools.browser_camofox.is_camofox_mode", return_value=False), \
         patch("tools.browser_tool_session._shares_bot_desktop_browser", return_value=False), \
         patch.object(bt, "_last_session_key", side_effect=lambda task: remap.get(task, task)), \
         patch("agent.vault_backends.backend_for_handle", return_value=Backend()), \
         patch.object(browser_vault_tool, "_current_page_origin", return_value=ORIGIN), \
         patch.object(browser_vault_tool, "_eval_js",
                      side_effect=lambda _t, _e: {"success": True, "result": json.dumps(controls)}), \
         patch.object(browser_vault_tool, "_eval_js_secret",
                      side_effect=lambda _t, _e: {"success": True, "result": json.dumps({"filled": 1})}):
        try:
            assert json.loads(browser_vault_tool.browser_vault_fill(meta.id, task_id="sidecar-task"))["success"]
            refusal = blocked_protected_date_pixels("sidecar-task")
            assert refusal is not None and "screenshot" in json.loads(refusal)["error"].lower()
            masked = json.loads(scrub_browser_result("browser_console", json.dumps({"result": 4}), "sidecar-task"))
            assert masked["result"] != 4
        finally:
            clear_vault_date_components("sidecar-task::private")
            clear_vault_date_components("sidecar-task")
