"""Protected birth-date fills require an owned, unreachable browser session."""
import json
from unittest.mock import Mock, patch

import pytest

from agent.redact import (
    clear_vault_date_components, has_vault_date_components, mark_vault_protected_tab,
)
from agent.vault_store import VaultItemMeta
from tools import browser_tool, browser_vault_tool
from tools.browser_cdp_tool import browser_cdp
from tools.browser_tool_lifecycle import _cleanup_single_browser_session
from tools.browser_tool_vision import blocked_protected_date_pixels


@pytest.mark.parametrize("kind", [
    "camofox", "managed_account", "cdp_override", "attached", "bot_desktop",
    "cloud", "real_profile", "lightpanda", "unowned", "no_session",
])
def test_protected_fill_refuses_shared_browser_before_resolving_secret(kind, monkeypatch):
    task = "private-check"
    session = {"session_key": task, "owner_task_id": task,
               "session_name": "local-private-check", "features": {"local": True}}
    if kind in ("cdp_override", "attached"):
        session.update(cdp_url="ws://127.0.0.1:9222", features={"cdp_override": True})
    if kind == "cloud":
        session.update(cdp_url="wss://cloud.example", bb_session_id="cloud-id", features={})
    if kind in ("real_profile", "lightpanda"):
        session["features"][kind] = True
    if kind == "unowned":
        session["owner_task_id"] = "other-task"
    monkeypatch.setattr(browser_tool, "_active_sessions", {} if kind == "no_session" else {task: session})
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "ws://127.0.0.1:9222" if kind == "cdp_override" else None)
    monkeypatch.setattr("tools.browser_camofox.is_camofox_mode", lambda: kind in ("camofox", "managed_account"))
    monkeypatch.setattr("tools.browser_tool_session._shares_bot_desktop_browser", lambda _: kind == "bot_desktop")
    meta = VaultItemMeta(id="fixture", kind="protected_field", label="Birth date",
                         origin="https://example.com", created_at="", field_token="bday",
                         allowed_origins=("https://example.com",))
    backend = Mock(needs_unlock=False)
    backend.get_meta.return_value = meta
    with patch("agent.vault_backends.backend_for_handle", return_value=backend):
        result = json.loads(browser_vault_tool.browser_vault_fill("fixture", task_id=task))
    assert result["error_type"] == "private_browser_required"
    assert "private per-task" in result["error"]
    backend.resolve_secret.assert_not_called()


def test_local_owned_session_permits_protected_fill(monkeypatch):
    task = "private-check"
    session = {"session_key": task, "owner_task_id": task, "session_name": "local-private-check",
               "features": {"local": True}}
    monkeypatch.setattr(browser_tool, "_active_sessions", {task: session})
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: None)
    monkeypatch.setattr("tools.browser_camofox.is_camofox_mode", lambda: False)
    monkeypatch.setattr("tools.browser_tool_session._shares_bot_desktop_browser", lambda _: False)
    meta = VaultItemMeta(id="fixture", kind="protected_field", label="Birth date",
                         origin="https://example.com", created_at="", field_token="bday",
                         allowed_origins=("https://example.com",))
    backend = Mock(display_name="1Password", needs_unlock=False,
                   binds_cards_to_page=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_secret.return_value = {"value": "1990-04-12"}
    monkeypatch.setattr(browser_tool, "_active_sessions", {task: {
        **session, "features": {"cdp_override": True}, "cdp_url": "ws://127.0.0.1:9222",
    }})
    with patch("agent.vault_backends.backend_for_handle", return_value=backend):
        assert json.loads(browser_vault_tool.browser_vault_fill("fixture", task_id=task))[
            "error_type"] == "private_browser_required"
    monkeypatch.setattr(browser_tool, "_active_sessions", {task: session})
    control = [{"autocomplete": "bday", "formIndex": 0, "index": 0,
                "label": "Date of birth", "name": "birthDate", "type": "date"}]
    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value=meta.origin), \
             patch.object(browser_vault_tool, "_eval_js", return_value={"success": True, "result": json.dumps(control)}), \
             patch.object(browser_vault_tool, "_eval_js_secret", return_value={"success": True, "result": '{"filled":1}'}) as write:
            result = json.loads(browser_vault_tool.browser_vault_fill("fixture", task_id=task))
        assert result["success"] and result["filled_fields"] == 1
        assert has_vault_date_components(task)
        write.assert_called_once()
    finally:
        clear_vault_date_components(task)


def test_hybrid_task_fills_only_the_checked_local_browser(monkeypatch):
    """Regression: a hybrid task keeps its cloud supervisor under the bare task id while its
    private-URL local sidecar has its own key. The privacy check approves the sidecar, so focus,
    inspection, write and redaction must all target the sidecar, never the cloud supervisor."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    task = "hybrid-task"
    local_key = f"{task}{browser_tool._LOCAL_SUFFIX}"
    local = {"session_key": local_key, "owner_task_id": task, "session_name": "local-hybrid",
             "features": {"local": True}}
    cloud = {"session_key": task, "owner_task_id": task, "cdp_url": "wss://cloud.example",
             "bb_session_id": "cloud-id", "features": {}}
    monkeypatch.setattr(browser_tool, "_active_sessions", {task: cloud, local_key: local})
    monkeypatch.setattr(browser_tool, "_last_active_session_key", {task: local_key})
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: None)
    monkeypatch.setattr("tools.browser_camofox.is_camofox_mode", lambda: False)
    monkeypatch.setattr("tools.browser_tool_session._shares_bot_desktop_browser", lambda _: False)

    control = [{"autocomplete": "bday", "formIndex": 0, "index": 0,
                "label": "Date of birth", "name": "birthDate", "type": "date"}]

    def supervisor(label):
        sup = Mock(name=label)
        sup.focus_page.return_value = {"ok": True, "url": "https://example.com/"}

        def evaluate(expression, **_):
            if expression == "window.location.href":
                return {"ok": True, "result": "https://example.com/form"}
            if "1990" in expression:
                return {"ok": True, "result": '{"filled":1}'}
            return {"ok": True, "result": json.dumps(control)}
        sup.evaluate_runtime.side_effect = evaluate
        return sup

    cloud_sup, local_sup = supervisor("cloud"), supervisor("local")
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "_by_task", {task: cloud_sup, local_key: local_sup})
    meta = VaultItemMeta(id="fixture", kind="protected_field", label="Birth date",
                         origin="https://example.com", created_at="", field_token="bday",
                         allowed_origins=("https://example.com",))
    backend = Mock(display_name="1Password", needs_unlock=False, binds_cards_to_page=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_secret.return_value = {"value": "1990-04-12"}
    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend):
            result = json.loads(browser_vault_tool.browser_vault_fill("fixture", task_id=task))
        assert result["success"] and result["filled_fields"] == 1
        written = [c.args[0] for c in local_sup.evaluate_runtime.call_args_list if "1990" in c.args[0]]
        assert len(written) == 1
        cloud_sup.evaluate_runtime.assert_not_called()
        cloud_sup.focus_page.assert_not_called()
        assert has_vault_date_components(local_key)
        assert not has_vault_date_components(task)
    finally:
        clear_vault_date_components(local_key)
        clear_vault_date_components(task)


def test_protected_fill_refuses_when_selected_browser_changes_before_write(monkeypatch):
    """The browser pinned at the start of the fill must still be the task's selection at the write."""
    task = "switch-task"
    local_key = f"{task}{browser_tool._LOCAL_SUFFIX}"
    local = {"session_key": local_key, "owner_task_id": task, "session_name": "local-switch",
             "features": {"local": True}}
    cloud = {"session_key": task, "owner_task_id": task, "cdp_url": "wss://cloud.example",
             "bb_session_id": "cloud-id", "features": {}}
    last = {task: local_key}
    monkeypatch.setattr(browser_tool, "_active_sessions", {task: cloud, local_key: local})
    monkeypatch.setattr(browser_tool, "_last_active_session_key", last)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: None)
    monkeypatch.setattr("tools.browser_camofox.is_camofox_mode", lambda: False)
    monkeypatch.setattr("tools.browser_tool_session._shares_bot_desktop_browser", lambda _: False)
    control = [{"autocomplete": "bday", "formIndex": 0, "index": 0,
                "label": "Date of birth", "name": "birthDate", "type": "date"}]

    def inspect(key, _expr):
        assert key == local_key
        last[task] = task  # the task navigates its cloud browser while the fill is in flight
        return {"success": True, "result": json.dumps(control)}

    meta = VaultItemMeta(id="fixture", kind="protected_field", label="Birth date",
                         origin="https://example.com", created_at="", field_token="bday",
                         allowed_origins=("https://example.com",))
    backend = Mock(display_name="1Password", needs_unlock=False, binds_cards_to_page=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_secret.return_value = {"value": "1990-04-12"}
    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(browser_vault_tool, "_focus_bound_origin", return_value=meta.origin), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=inspect), \
             patch.object(browser_vault_tool, "_eval_js_secret") as write:
            result = json.loads(browser_vault_tool.browser_vault_fill("fixture", task_id=task))
        assert result["error_type"] == "private_browser_required"
        write.assert_not_called()
    finally:
        clear_vault_date_components(local_key)
        clear_vault_date_components(task)


@pytest.mark.parametrize("method", ["Page.captureScreenshot", "Runtime.evaluate", "Target.getTargets"])
@pytest.mark.parametrize("route", ["target", "frame"])
def test_other_task_cannot_use_raw_cdp_while_browser_protected(method, route):
    mark_vault_protected_tab("task-a", "https://example.com")
    try:
        with patch("tools.browser_cdp_tool._resolve_cdp_endpoint", side_effect=AssertionError("endpoint reached")), \
             patch("tools.browser_cdp_tool._browser_cdp_via_supervisor", side_effect=AssertionError("supervisor reached")):
            result = json.loads(browser_cdp(method, task_id="task-b",
                                            target_id="protected-tab" if route == "target" else None,
                                            frame_id="protected-frame" if route == "frame" else None))
        assert result.get("success") is not True
        assert "protected" in result["error"].lower()
    finally:
        clear_vault_date_components("task-a")


def test_timeout_does_not_discard_protected_browser(monkeypatch, tmp_path):
    from tools.browser_tool_session import _discard_timed_out_browser_session
    task = "timed-out"
    session = {"session_key": task, "owner_task_id": task,
               "session_name": "local-timed-out", "features": {"local": True}}
    monkeypatch.setattr(browser_tool, "_active_sessions", {task: session})
    mark_vault_protected_tab(task, "https://example.com")
    try:
        _discard_timed_out_browser_session(task, session, str(tmp_path))
        assert browser_tool._active_sessions[task] is session
        assert blocked_protected_date_pixels(task) is not None
    finally:
        clear_vault_date_components(task)


@pytest.mark.parametrize("close_result", [RuntimeError("close failed"), {"success": False}])
def test_failed_close_keeps_pixels_blocked_and_session_tracked(close_result, monkeypatch):
    task = "close-check"
    session = {"session_key": task, "owner_task_id": task,
               "session_name": "local-close-check", "features": {"local": True}}
    monkeypatch.setattr(browser_tool, "_active_sessions", {task: session})
    monkeypatch.setattr("tools.browser_tool_lifecycle._session_has_expired", lambda _: False)
    mark_vault_protected_tab(task, "https://example.com")
    try:
        with patch("tools.browser_tool_lifecycle._cdp._stop_cdp_supervisor"), \
             patch("tools.browser_tool_lifecycle._bt._maybe_stop_recording"), \
             patch("tools.browser_tool_lifecycle._session._run_browser_command", side_effect=close_result if isinstance(close_result, Exception) else None, return_value=close_result):
            _cleanup_single_browser_session(task)
        assert has_vault_date_components(task)
        assert browser_tool._active_sessions[task] is session
        assert blocked_protected_date_pixels(task) is not None
        with patch("tools.browser_tool_lifecycle._cdp._stop_cdp_supervisor"), \
             patch("tools.browser_tool_lifecycle._bt._maybe_stop_recording"), \
             patch("tools.browser_tool_lifecycle._session._run_browser_command", return_value={"success": True}), \
             patch("tools.browser_tool_lifecycle._release_session_resources"):
            _cleanup_single_browser_session(task)
        assert not has_vault_date_components(task)
        assert blocked_protected_date_pixels(task) is None
    finally:
        clear_vault_date_components(task)
