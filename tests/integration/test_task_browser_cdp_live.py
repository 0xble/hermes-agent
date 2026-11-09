"""Opt-in real Chromium/CDP + agent-browser dispatch proof (no personal browser).

HERMES_TEST_LIVE_TASK_CDP=1 .venv/bin/python -m pytest \
    tests/integration/test_task_browser_cdp_live.py -q
Requires installed Playwright Chromium and agent-browser; never installs either.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from unittest.mock import Mock
from urllib.request import urlopen

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_TEST_LIVE_TASK_CDP") != "1",
    reason="explicit live task CDP proof only",
)


def test_real_dispatch_and_detach_leave_external_chromium_alive(monkeypatch, tmp_path):
    from playwright.sync_api import sync_playwright
    from tools import browser_tool as bt
    from tools import browser_tool_session as session
    from tools.browser_task_cdp import attach_task_browser, detach_task_browser
    from tools.registry import registry

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
    try:
        deadline = time.monotonic() + 15
        port_file = profile / "DevToolsActivePort"
        while not port_file.exists():
            assert proc.poll() is None, "owned fixture Chromium exited early"
            assert time.monotonic() < deadline, "owned fixture CDP startup timed out"
            time.sleep(0.05)
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
        assert detach_task_browser(task)
        assert proc.poll() is None, "detach killed externally owned Chromium"
        with urlopen(version_url, timeout=5) as response:
            assert json.load(response)["webSocketDebuggerUrl"] == endpoint
        assert dispatch("browser_snapshot", {})["backend"] == "camofox"
        print("LIVE PROOF: registry navigate/eval/snapshot reached real CDP; other task stayed Camofox; detach preserved Chromium and restored Camofox")
    finally:
        detach_task_browser(task)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
