#!/usr/bin/env python3
"""Live named-Camofox E2E against synthetic data and a temporary Hermes home.

By default this proves identity isolation and a fresh-interpreter follow-up.  Pass
``--restart-service`` to additionally prove persisted storage survives an actual
Camofox launchd restart.  That mode refuses to restart while any non-E2E user has
an active tab and deletes all synthetic server persistence before it exits.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CAMOFOX_URL = "http://127.0.0.1:9377"
LAUNCHD_LABEL = "com.brianle.camofox"


def result(call, *args, **kwargs) -> dict[str, Any]:
    value = json.loads(call(*args, **kwargs))
    assert isinstance(value, dict), value
    assert value.get("success"), value
    return value


def camofox_api(method: str, path: str, key: str, **kwargs) -> dict[str, Any]:
    import requests
    response = requests.request(method, f"{CAMOFOX_URL}{path}", headers={"Authorization": f"Bearer {key}"}, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def page_state(handle_function_call, task_id: str) -> dict[str, Any]:
    expression = "JSON.stringify({cookie:document.cookie, local:localStorage.getItem('named-e2e')})"
    payload = result(handle_function_call, "browser_console", {"expression": expression}, task_id=task_id)
    raw = payload.get("result")
    state = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(state, dict), state
    return state


def set_page_state(handle_function_call, task_id: str, label: str) -> dict[str, Any]:
    expression = ("(() => { document.cookie = 'named-e2e=" + label + "; path=/'; "
                  "localStorage.setItem('named-e2e', '" + label + "'); "
                  "return JSON.stringify({cookie:document.cookie, local:localStorage.getItem('named-e2e')}); })()")
    payload = result(handle_function_call, "browser_console", {"expression": expression}, task_id=task_id)
    raw = payload.get("result")
    state = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(state, dict), state
    assert label in state["cookie"] and state["local"] == label, state
    return state


def assert_server_storage(key: str, user_id: str, label: str) -> None:
    state = camofox_api("GET", f"/sessions/{user_id}/storage_state", key)
    cookies = state.get("cookies", [])
    origins = state.get("origins", [])
    assert any(cookie.get("name") == "named-e2e" and cookie.get("value") == label for cookie in cookies), state
    assert any(any(item.get("name") == "named-e2e" and item.get("value") == label
                   for item in origin.get("localStorage", [])) for origin in origins), state


def restart_service(key: str, allowed_users: set[str]) -> None:
    import requests
    tabs = camofox_api("GET", "/tabs", key).get("tabs", [])
    active_users = {str(tab.get("userId")) for tab in tabs if isinstance(tab, dict) and tab.get("userId")}
    unsafe = active_users - allowed_users
    if unsafe:
        raise RuntimeError(f"refusing to restart Camofox with non-test active users: {sorted(unsafe)}")
    # launchctl is macOS-only, so os.getuid() is reachable wherever this line runs.
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=True, timeout=30)  # windows-footgun: ok
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if camofox_api("GET", "/health", key).get("ok"):
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise AssertionError("Camofox did not become healthy after launchd restart")


def resume_followup(task_id: str, label: str, page: str) -> None:
    """Fresh interpreter follow-up: verifies durable binding and persisted state."""
    from model_tools import handle_function_call

    result(handle_function_call, "browser_navigate", {"url": page}, task_id=task_id)
    state = page_state(handle_function_call, task_id)
    assert label in state["cookie"] and state["local"] == label, state
    print(json.dumps({"success": True, "restart_followup": True, "task": task_id}))


def cleanup_synthetic_state(key: str, home: Path, task_ids, created_users: set[str], server) -> None:
    """Attempt each known synthetic identity, retaining recovery evidence on failure."""
    from tools.browser_camofox_state import read_camofox_binding

    unresolved = []
    unreadable_tasks = []
    users = set(created_users)
    try:
        # A failed first navigation may have committed its binding before returning.
        for task_id in task_ids:
            try:
                binding = read_camofox_binding(task_id)
                if binding:
                    users.add(binding["user_id"])
            except Exception:
                unreadable_tasks.append(task_id)
        for user_id in sorted(users):
            try:
                camofox_api("DELETE", f"/sessions/{user_id}/storage_state", key)
            except Exception:
                unresolved.append(user_id)
    finally:
        try:
            server.shutdown()
        finally:
            server.server_close()
    if unresolved or unreadable_tasks:
        receipt = home / "cleanup-recovery.json"
        receipt.write_text(json.dumps({"success": False, "unresolved_user_ids": unresolved,
                                      "unreadable_task_ids": unreadable_tasks}, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError(f"Synthetic Camofox cleanup incomplete; recovery receipt retained at {receipt}")
    shutil.rmtree(home)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-service", action="store_true", help="restart launchd Camofox after refusing non-test active users")
    parser.add_argument("--resume", nargs=3, metavar=("TASK", "LABEL", "PAGE"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.resume:
        resume_followup(*args.resume)
        return

    spec = spec_from_file_location("camofox_launch", "/Users/brianle/.local/share/camofox/launch.py")
    assert spec and spec.loader
    launch = module_from_spec(spec)
    spec.loader.exec_module(launch)
    key = launch.credential()
    os.environ.update({"CAMOFOX_API_KEY": key, "CAMOFOX_URL": CAMOFOX_URL})
    home = Path(tempfile.mkdtemp(prefix="hermes-camofox-named-e2e-"))
    os.environ["HERMES_HOME"] = str(home)
    tag = uuid.uuid4().hex[:10]
    labels = ("personal", "lpg", "meridian")
    task = {label: f"e2e-{label}-{tag}" for label in labels}
    sibling_task = f"e2e-personal-sibling-{tag}"
    created_users: set[str] = set()
    home.joinpath("config.yaml").write_text(
        "browser:\n  cloud_provider: camofox\n  real_profile_identities:\n"
        "    personal: {browser: chrome, source_profile: Default}\n"
        "    lpg: {browser: chrome, source_profile: 'Profile 1'}\n"
        "    meridian: {browser: chrome, source_profile: 'Profile 2'}\n",
        encoding="utf-8",
    )
    webroot = home / "fixture"
    webroot.mkdir()
    webroot.joinpath("index.html").write_text("<title>Named Camofox E2E</title><input aria-label='field'><button>go</button>", encoding="utf-8")

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: QuietHandler(*a, directory=str(webroot), **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page = f"http://127.0.0.1:{server.server_port}/index.html"
    try:
        from model_tools import handle_function_call
        from tools import browser_camofox
        from tools.browser_camofox_state import read_camofox_binding

        bindings: dict[str, dict[str, str]] = {}
        for label in labels:
            nav = result(handle_function_call, "browser_navigate", {"url": page, "identity": label}, task_id=task[label])
            assert nav.get("element_count", 0) >= 1, nav
            set_page_state(handle_function_call, task[label], label)
            binding = read_camofox_binding(task[label])
            assert binding and binding["alias"] == label, binding
            bindings[label] = binding
            created_users.add(binding["user_id"])
            assert_server_storage(key, binding["user_id"], label)

        assert len({binding["user_id"] for binding in bindings.values()}) == len(labels), bindings
        for label in labels:
            state = page_state(handle_function_call, task[label])
            assert label in state["cookie"] and state["local"] == label, state

        # Same alias gets a distinct tab. Closing one task must not kill its sibling.
        result(handle_function_call, "browser_navigate", {"url": page, "identity": "personal"}, task_id=sibling_task)
        sibling_binding = read_camofox_binding(sibling_task)
        assert sibling_binding and sibling_binding["user_id"] == bindings["personal"]["user_id"]
        closed = result(browser_camofox.camofox_close, task["personal"])
        assert closed["closed"]
        sibling_state = page_state(handle_function_call, sibling_task)
        assert "personal" in sibling_state["cookie"] and sibling_state["local"] == "personal", sibling_state

        # Interpreter restart always runs; optional launchd restart exercises service persistence too.
        if args.restart_service:
            restart_service(key, created_users)
        with browser_camofox._sessions_lock:
            browser_camofox._sessions.clear()
        for label in labels:
            resumed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--resume", task[label], label, page],
                                     env=os.environ.copy(), capture_output=True, text=True, timeout=120)
            assert resumed.returncode == 0, resumed.stderr or resumed.stdout
            assert json.loads(resumed.stdout).get("restart_followup"), resumed.stdout

        # Native registry refuses unbound, missing, unknown, and warm overridden identities.
        missing = json.loads(handle_function_call("browser_navigate", {"url": page}, task_id=f"missing-{tag}"))
        unknown = json.loads(handle_function_call("browser_navigate", {"url": page, "identity": "unknown"}, task_id=f"unknown-{tag}"))
        os.environ["CAMOFOX_USER_ID"] = "forbidden-warm-override"
        warm = json.loads(handle_function_call("browser_snapshot", {}, task_id=task["lpg"]))
        os.environ.pop("CAMOFOX_USER_ID", None)
        assert not missing.get("success") and "identity" in missing.get("error", ""), missing
        assert not unknown.get("success") and "identity" in unknown.get("error", "").lower(), unknown
        assert not warm.get("success") and "CAMOFOX_USER_ID" in warm.get("error", ""), warm
    finally:
        os.environ.pop("CAMOFOX_USER_ID", None)
        cleanup_synthetic_state(key, home, [*task.values(), sibling_task], created_users, server)
    print(json.dumps({"success": True, "identity_isolation": True, "sibling_survival": True,
                      "interpreter_restart": True, "service_restart": args.restart_service}))


if __name__ == "__main__":
    main()
