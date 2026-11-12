"""Opt-in real Chromium/CDP + agent-browser dispatch proof (no personal browser).

env -u __HERMES_ACTIVATED HERMES_PYTHON="$PWD/.venv/bin/python" \
    HERMES_TEST_LIVE_TASK_CDP=1 scripts/run_tests.sh \
    tests/integration/test_task_browser_cdp_live.py -q -s
Requires installed Playwright Chromium and agent-browser; never installs either.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock
from urllib.request import urlopen

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_TEST_LIVE_TASK_CDP") != "1",
    reason="explicit live task CDP proof only",
)


@pytest.mark.live_system_guard_bypass  # Only owned fixture Chromium/agent-browser may be signaled.
def test_real_dispatch_and_detach_leave_external_chromium_alive(monkeypatch, tmp_path, request):
    from playwright.sync_api import sync_playwright
    from tools import browser_tool as bt
    from tools import browser_tool_session as session
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser
    from tools.registry import registry
    from tests.waiting import process_alive, wait_until
    import psutil

    cli = shutil.which("agent-browser")
    assert cli, "agent-browser must already be installed"
    with sync_playwright() as pw:
        chrome = Path(pw.chromium.executable_path)
    assert chrome.is_file(), "Playwright Chromium must already be installed"
    profile = tmp_path / "owned-chromium"
    profile.mkdir()
    proc = subprocess.Popen(
        [str(chrome), "--headless", "--remote-debugging-port=0", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    task = "owned-live-task-cdp-fixture"

    def cleanup():
        try:
            detach_task_browser(task)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    # Register before startup assertions/attachment so failures cannot skip cleanup.
    request.addfinalizer(cleanup)
    port_file = profile / "DevToolsActivePort"

    def started():
        assert proc.poll() is None, "owned fixture Chromium exited early"
        return port_file.exists()

    wait_until(started, timeout=15, what="owned fixture CDP startup")
    port, ws_path = port_file.read_text().splitlines()[:2]
    endpoint = f"ws://127.0.0.1:{port}{ws_path}"
    version_url = f"http://127.0.0.1:{port}/json/version"
    with urlopen(version_url, timeout=5) as response:
        assert json.load(response)["webSocketDebuggerUrl"] == endpoint
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session, "_browser_command_preflight", lambda: {"browser_cmd": cli})
    camofox = Mock(return_value=json.dumps({"success": True, "backend": "camofox"}))
    monkeypatch.setattr(bt, "_camofox", camofox)

    def dispatch(tool, args, runtime_task=task):
        result = registry.dispatch(tool, args, task_id=runtime_task)
        return json.loads(result) if isinstance(result, str) else result

    attach_task_browser(task, endpoint)
    assert dispatch("browser_navigate", {"url": "about:blank"})["success"]
    result = dispatch("browser_console", {"expression": "document.title = 'owned live task CDP fixture'; document.title"})
    assert result["success"], result
    assert result["result"] == "owned live task CDP fixture"
    assert dispatch("browser_snapshot", {})["success"]
    camofox.assert_not_called()
    assert dispatch("browser_click", {"ref": "@e1"}, "other-owned-fixture")["backend"] == "camofox"
    info = bt._active_sessions[task]
    socket_dir = Path(bt._socket_safe_tmpdir()) / f"agent-browser-{info['session_name']}"
    daemon = psutil.Process(int((socket_dir / f"{info['session_name']}.pid").read_text()))
    assert detach_task_browser(task)
    wait_until(lambda: not process_alive(daemon), timeout=10, what="owned agent-browser daemon exit")
    assert not socket_dir.exists(), "detach leaked owned agent-browser socket directory"
    assert proc.poll() is None, "detach killed externally owned Chromium"
    with urlopen(version_url, timeout=5) as response:
        assert json.load(response)["webSocketDebuggerUrl"] == endpoint
    assert dispatch("browser_snapshot", {})["backend"] == "camofox"
    print("LIVE PROOF: registry navigate/eval/snapshot reached real CDP; other task stayed Camofox; detach reaped its daemon/socket directory, preserved Chromium and restored Camofox")
