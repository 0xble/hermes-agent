"""Task CDP routing is exercised through the real registry, not a session-map seam."""
import json
from unittest.mock import Mock

import pytest

from tools import browser_tool as bt
from tools import browser_tool_session as session
from tools.registry import registry

ENDPOINT = "ws://127.0.0.1:19222/devtools/browser/owned-fixture"


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr(bt, "_session_last_activity", {})
    monkeypatch.setattr(bt, "_session_owner_homes", {})
    monkeypatch.setattr(bt, "_suspect_browser_sessions", {})
    monkeypatch.setattr(bt, "_last_active_session_key", {})
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    camofox = Mock(return_value=json.dumps({"success": True, "backend": "camofox"}))
    monkeypatch.setattr(bt, "_camofox", camofox)
    monkeypatch.setattr("tools.browser_camofox.camofox_soft_cleanup", lambda task: True)
    monkeypatch.setattr("tools.browser_camofox.camofox_close", lambda task: None)
    argv = []

    def spawn(task_id, info, cmd_parts, *rest):
        argv.append((task_id, info.copy(), cmd_parts))
        return {"success": True, "data": {"url": "https://example.com", "snapshot": "fixture", "result": "https://example.com"}}

    monkeypatch.setattr(session, "_spawn_and_collect", spawn)
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    supervisor = Mock()
    supervisor.get_or_start.return_value = None
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get_or_start", supervisor.get_or_start)
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda task_id: None)
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "stop", supervisor.stop)
    return argv, camofox, supervisor


def dispatch(name, args, task_id):
    result = registry.dispatch(name, args, task_id=task_id)
    return json.loads(result) if isinstance(result, str) else result


def test_session_cdp_url_alone_does_not_override_camofox(boundary):
    argv, camofox, _ = boundary
    bt._active_sessions["legacy"] = {"session_name": "existing", "cdp_url": ENDPOINT}
    assert dispatch("browser_click", {"ref": "@e1"}, "legacy")["backend"] == "camofox"
    assert not argv
    camofox.assert_called_once()


def test_registry_dispatch_isolates_external_task_and_restores_camofox(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser
    argv, camofox, supervisor = boundary
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override_raw", lambda: "ws://global.invalid/devtools/browser/global")
    attach_task_browser("external", ENDPOINT)
    assert dispatch("browser_navigate", {"url": "https://example.com"}, "external")["success"]
    assert dispatch("browser_click", {"ref": "@e1"}, "external")["success"]
    assert not camofox.called
    assert argv
    for task_id, info, cmd in argv:
        assert task_id == "external"
        assert info["cdp_url"] == ENDPOINT
        assert cmd[cmd.index("--cdp") + 1] == ENDPOINT
        assert cmd[cmd.index("--session") + 1] == info["session_name"]
    assert supervisor.get_or_start.call_args.kwargs["task_id"] == "external"
    assert supervisor.get_or_start.call_args.kwargs["cdp_url"] == ENDPOINT
    assert dispatch("browser_navigate", {"url": "https://example.com"}, "other")["backend"] == "camofox"
    assert camofox.call_args.args[-2] == "other"
    assert detach_task_browser("external") is True
    assert detach_task_browser("external") is False
    assert dispatch("browser_click", {"ref": "@e1"}, "external")["backend"] == "camofox"
    assert not any("close" in cmd for _, _, cmd in argv)
    supervisor.stop.assert_called_with("external")


@pytest.mark.parametrize("name,args", [
    ("browser_snapshot", {}), ("browser_click", {"ref": "@e1"}),
    ("browser_type", {"ref": "@e1", "text": "ordinary fixture text"}),
    ("browser_scroll", {"direction": "down"}), ("browser_back", {}),
    ("browser_press", {"key": "Escape"}), ("browser_console", {}),
    ("browser_console", {"expression": "document.title"}),
    ("browser_get_images", {}),
])
def test_registry_actions_never_fall_back_to_camofox(boundary, name, args):
    from tools.browser_task_cdp import attach_task_browser
    argv, camofox, _ = boundary
    attach_task_browser("external", ENDPOINT)
    result = dispatch(name, args, "external")
    assert result.get("success"), result
    assert argv
    camofox.assert_not_called()


@pytest.mark.parametrize("name,args", [
    ("browser_tabs", {}), ("browser_handoff", {"account": "fixture"}),
    ("browser_upload", {"paths": ["/missing"]}), ("browser_downloads", {}),
])
def test_camofox_only_tools_fail_closed_for_attached_task(boundary, name, args):
    from tools.browser_task_cdp import attach_task_browser
    _, camofox, _ = boundary
    attach_task_browser("external", ENDPOINT)
    result = dispatch(name, args, "external")
    assert not result.get("success"), result
    camofox.assert_not_called()


def test_attach_validation_idempotence_and_existing_session_refusal(boundary):
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser
    for task in (None, "", " ", "default", "task::local"):
        with pytest.raises(ValueError):
            attach_task_browser(task, ENDPOINT)
    for endpoint in ("", "9222", "file:///missing", "ws://"):
        with pytest.raises(ValueError):
            attach_task_browser("external", endpoint)
    attach_task_browser("external", ENDPOINT)
    original = bt._active_sessions["external"]
    attach_task_browser("external", ENDPOINT)
    assert bt._active_sessions["external"] is original
    with pytest.raises(RuntimeError):
        attach_task_browser("external", "ws://127.0.0.1:19223")
    bt._active_sessions["existing"] = {"session_name": "preexisting"}
    with pytest.raises(RuntimeError):
        attach_task_browser("existing", ENDPOINT)
    assert not detach_task_browser("existing")
    assert "existing" in bt._active_sessions


def test_normal_cleanup_never_sends_close_to_external_browser(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser
    argv, _, supervisor = boundary
    camo_cleanup = Mock()
    monkeypatch.setattr("tools.browser_camofox.camofox_soft_cleanup", camo_cleanup)
    attach_task_browser("external", ENDPOINT)
    bt._lifecycle.cleanup_browser("external")
    assert "external" not in bt._active_sessions
    assert not argv
    supervisor.stop.assert_called_once_with("external")
    camo_cleanup.assert_called_once_with("external")


def test_quarantined_attachment_cannot_be_silently_detached(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser, task_browser_url
    attach_task_browser("external", ENDPOINT)
    monkeypatch.setattr("agent.redact.has_vault_date_components", lambda task: True)
    assert not detach_task_browser("external")
    bt._lifecycle.cleanup_browser("external")
    assert task_browser_url("external") == ENDPOINT


def test_inactivity_reaper_preserves_attachment_and_dispatch_route(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser, task_browser_url
    argv, camofox, _ = boundary
    monkeypatch.setattr(bt, "BROWSER_SESSION_INACTIVITY_TIMEOUT", 0.01)
    attach_task_browser("external", ENDPOINT)
    bt._session_last_activity["external"] -= 1
    # The same janitor pass must still retire an ordinary idle task.
    bt._session_last_activity["ordinary"] = bt._session_last_activity["external"]
    bt._lifecycle._cleanup_inactive_browser_sessions()
    assert "ordinary" not in bt._session_last_activity
    assert task_browser_url("external") == ENDPOINT
    assert dispatch("browser_click", {"ref": "@e1"}, "external")["success"]
    assert argv[-1][1]["cdp_url"] == ENDPOINT
    camofox.assert_not_called()
    assert detach_task_browser("external")
    assert dispatch("browser_click", {"ref": "@e1"}, "external")["backend"] == "camofox"


def test_timeout_replacement_keeps_external_routing(boundary, tmp_path):
    from tools.browser_task_cdp import attach_task_browser, task_browser_url
    attach_task_browser("external", ENDPOINT)
    original = bt._active_sessions["external"]
    session._discard_timed_out_browser_session("external", original, str(tmp_path))
    assert task_browser_url("external") == ENDPOINT
    assert bt._active_sessions["external"]["session_name"] != original["session_name"]
    assert dispatch("browser_click", {"ref": "@e1"}, "external")["success"]
    boundary[1].assert_not_called()


def test_attachment_is_not_visible_or_detachable_from_another_profile(boundary):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser, task_browser_url
    attach_task_browser("external", ENDPOINT)
    token = set_hermes_home_override("/fixture/other-profile")
    try:
        assert task_browser_url("external") is None
        assert not detach_task_browser("external")
        with pytest.raises(RuntimeError):
            attach_task_browser("external", ENDPOINT)
    finally:
        reset_hermes_home_override(token)
    assert task_browser_url("external") == ENDPOINT


def test_external_navigation_and_eval_keep_nonlocal_url_guards(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser
    argv, camofox, _ = boundary
    monkeypatch.setattr(bt._cloud, "_is_local_backend", lambda: True)
    monkeypatch.setattr(bt._cloud, "_allow_private_urls", lambda: False)
    attach_task_browser("external", ENDPOINT)
    result = dispatch("browser_navigate", {"url": "http://127.0.0.1/private"}, "external")
    assert not result["success"]
    assert "private" in result["error"]
    result = dispatch("browser_console", {"expression": "fetch('http://127.0.0.1/private')"}, "external")
    assert not result["success"]
    assert not argv
    camofox.assert_not_called()


def test_external_blank_navigation_snapshot_and_eval_keep_other_schemes_blocked(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser
    argv, camofox, _ = boundary
    monkeypatch.setattr(bt._cloud, "_allow_private_urls", lambda: False)
    attach_task_browser("external", ENDPOINT)
    assert dispatch("browser_navigate", {"url": "about:blank"}, "external")["success"]
    assert any("open" in cmd and "about:blank" in cmd for _, _, cmd in argv)
    spawn = session._spawn_and_collect

    def on_blank(task_id, info, cmd_parts, *rest):
        result = spawn(task_id, info, cmd_parts, *rest)
        if "window.location.href" in cmd_parts:
            result["data"]["result"] = "about:blank"
        return result

    monkeypatch.setattr(session, "_spawn_and_collect", on_blank)
    assert dispatch("browser_snapshot", {}, "external")["success"]
    assert dispatch("browser_console", {"expression": "document.title"}, "external")["success"]
    argv.clear()
    for url in ("about:config", "about:blank#fragment", "file:///etc/hosts", "data:text/html,fixture", "javascript:alert(1)"):
        assert not dispatch("browser_navigate", {"url": url}, "external")["success"]
    assert not argv
    camofox.assert_not_called()


@pytest.mark.parametrize("action", ["list", "unlock", "fill", "save_login", "enter_code"])
def test_vault_fails_closed_before_any_parked_browser_or_vault_access(boundary, monkeypatch, action):
    from tools import browser_vault_tool as vault
    from tools.browser_task_cdp import attach_task_browser
    access = Mock(side_effect=AssertionError("attached tasks must not access the vault or parked browser"))
    for name in ("browser_vault_list", "browser_vault_unlock", "_fenced_page_op"):
        monkeypatch.setattr(vault, name, access)
    attach_task_browser("external", ENDPOINT)
    result = dispatch("browser_vault", {"action": action, "handle": "fixture"}, "external")
    assert result.get("success") is False
    assert result["error_type"] == "unsupported_backend"
    assert "attached" in result["error"]
    access.assert_not_called()
    boundary[1].assert_not_called()


def test_broken_endpoint_does_not_fall_back_or_close(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser
    _, camofox, _ = boundary
    monkeypatch.setattr(session, "_spawn_and_collect", lambda *a: {"success": False, "error": "fixture CDP unavailable"})
    attach_task_browser("external", ENDPOINT)
    result = dispatch("browser_navigate", {"url": "https://example.com"}, "external")
    assert not result["success"]
    assert "CDP unavailable" in result["error"]
    camofox.assert_not_called()
    assert detach_task_browser("external")


def test_attachment_bypasses_extension_controller_and_global_engine(boundary, monkeypatch):
    from tools.browser_task_cdp import attach_task_browser
    argv, camofox, _ = boundary
    monkeypatch.setattr(bt._cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr("gateway.browser_control_broker.browser_control_enabled", lambda: True)
    broker = Mock(side_effect=AssertionError("attached task must not consult extension controller"))
    monkeypatch.setattr("gateway.browser_control_broker.get_browser_control_broker", broker)
    attach_task_browser("external", ENDPOINT)
    assert dispatch("browser_click", {"ref": "@e1"}, "external")["success"]
    assert "--engine" not in argv[0][2]
    broker.assert_not_called()
    camofox.assert_not_called()


def test_vision_uses_external_screenshot_not_camofox(boundary, monkeypatch, tmp_path):
    from tools.browser_task_cdp import attach_task_browser
    argv, camofox, _ = boundary
    monkeypatch.setattr("hermes_constants.get_hermes_dir", lambda *a: tmp_path)
    attach_task_browser("external", ENDPOINT)
    # Capture is stubbed and writes no image; failure must stay on the attached
    # endpoint, never "rescue" through Camofox or a different local browser.
    result = dispatch("browser_vision", {"question": "fixture"}, "external")
    assert not result["success"]
    assert any("screenshot" in cmd for _, _, cmd in argv)
    camofox.assert_not_called()
