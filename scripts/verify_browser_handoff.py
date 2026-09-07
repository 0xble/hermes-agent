#!/usr/bin/env python3
"""Live, disposable proof of the gated managed-browser visibility path."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def chrome_binary() -> str:
    candidate = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(candidate).is_file():
        raise RuntimeError("Google Chrome is required for --live verification")
    return candidate


def endpoint(port: int) -> str:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                return json.load(response)["webSocketDebuggerUrl"]
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("disposable Chrome did not expose loopback CDP")


def verify() -> None:
    # Set before importing Hermes configuration modules; nothing touches a real home/profile.
    disposable_home = Path(tempfile.mkdtemp(prefix="hermes-visibility-home-"))
    os.environ["HERMES_HOME"] = str(disposable_home)
    config = disposable_home / "config.yaml"
    config.write_text(
        "browser:\n"
        "  use_real_profile: true\n"
        "  visibility_handoff: true\n"
        "  real_profile_identities:\n"
        "    disposable:\n"
        "      browser: chrome\n"
        "      source_profile: Default\n",
        encoding="utf-8",
    )
    from hermes_cli.browser_connect import real_profile_copy_dir
    from hermes_cli.browser_identity import resolve_browser_identity
    from tools import browser_use_cli
    from tools.browser_handoff_cdp import HandoffCDP

    session = "visibility-proof"
    copy_dir = Path(real_profile_copy_dir("chrome", "disposable", "Default"))
    copy_dir.mkdir(parents=True)
    (copy_dir / ".hermes-browser-mode").write_text("headed\n", encoding="utf-8")
    identity = resolve_browser_identity("disposable")
    if identity is None:
        raise RuntimeError("disposable named identity was not resolved")
    owner = browser_use_cli._browser_exec_runtime_owner(identity)
    # The production dispatch requires an existing immutable named-session binding.
    claimed = browser_use_cli._claim_browser_exec_durable_binding(session, owner)
    if claimed != owner:
        raise RuntimeError("could not establish disposable session binding")

    process = subprocess.Popen([
        chrome_binary(), f"--user-data-dir={copy_dir}", "--remote-debugging-port=0",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        port_file = copy_dir / "DevToolsActivePort"
        while not port_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("disposable Chrome exited before exposing CDP")
            time.sleep(0.05)
        port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
        with HandoffCDP(endpoint(port)) as cdp:
            cdp.assert_owned_headed_command_line(str(copy_dir))
            targets = cdp.call("Target.getTargets")["targetInfos"]
            page = next(target for target in targets if target["type"] == "page")
            attached = cdp.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
            session_id = attached["sessionId"]
            cdp.call("Runtime.evaluate", {"expression": "window.visibilityProof = 'retained'; document.body.innerHTML='<input id=proof value=retained>'"}, session_id=session_id)
        pid = process.pid
        enabled_config = config.read_text(encoding="utf-8")
        config.write_text(enabled_config.replace("visibility_handoff: true", "visibility_handoff: false"), encoding="utf-8")
        disabled = browser_use_cli.browser_exec("# disabled", session=session, local=True, identity="disposable", handoff="reveal")
        if "disabled" not in str(disabled):
            raise RuntimeError(f"disabled gate was not enforced: {disabled}")
        config.write_text(enabled_config, encoding="utf-8")
        minimized = browser_use_cli.browser_exec("# minimize", session=session, local=True, identity="disposable", handoff="minimize")
        revealed = browser_use_cli.browser_exec("# reveal", session=session, local=True, identity="disposable", handoff="reveal")
        if "minimized" not in str(minimized) or "normal" not in str(revealed) or process.poll() is not None or process.pid != pid:
            raise RuntimeError(f"visibility dispatch failed: {minimized!r}; {revealed!r}")
        with HandoffCDP(endpoint(port)) as cdp:
            targets = cdp.call("Target.getTargets")["targetInfos"]
            page_after = next(target for target in targets if target["type"] == "page")
            if page_after["targetId"] != page["targetId"]:
                raise RuntimeError("visibility changed the page target")
            attached = cdp.call("Target.attachToTarget", {"targetId": page_after["targetId"], "flatten": True})
            value = cdp.call("Runtime.evaluate", {"expression": "[window.visibilityProof, document.querySelector('#proof').value]", "returnByValue": True}, session_id=attached["sessionId"])
            if value["result"].get("value") != ["retained", "retained"]:
                raise RuntimeError("visibility did not preserve in-memory page state")
        print(f"PASS: integrated reveal/minimize retained target, JS/form state, and disposable Chrome PID {pid}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        shutil.rmtree(disposable_home, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    verify()
