"""Named nodes through the public dispatcher and real persistent MCP stdio.

Only SSH is replaced by an executable fixture. Provider selection, node config,
backend state, MCP SDK, capture parsing and native image delivery are real.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sys
import time

from PIL import Image
import pytest
# JSON is also valid YAML, so config fixtures need no second YAML implementation.

from tools.computer_use import tool
from tools.nodes import Node, NodeError, load_nodes, resolve_node


@contextmanager
def home(path):
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(path, prepared_secret_scope={}):
        yield


def config(path, default="studio", hosts=None):
    path.mkdir(exist_ok=True)
    (path / "config.yaml").write_text(json.dumps({
        "nodes": {"default": default, "hosts": hosts or {
            "studio": {"ssh": "studio-alias", "platform": "macos", "computer_use": {"permission_mode": "unrestricted"}},
            "book": {"ssh": "book-alias", "platform": "macos", "computer_use": {"permission_mode": "unrestricted"}}}},
        "computer_use": {"backend": "cua", "max_image_dimension": 64}}))


@pytest.fixture
def endpoint(tmp_path, monkeypatch, grant_computer_use_approvals):
    root = tmp_path / "bin"
    root.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "node_ssh.py"
    executable = root / "ssh"
    executable.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    executable.chmod(0o755)
    image = io.BytesIO()
    Image.new("RGB", (128, 80), "white").save(image, format="PNG")
    state = {"image": base64.b64encode(image.getvalue()).decode()}

    def update(**values):
        state.update(values)
        (root / "state.json").write_text(json.dumps(state))

    update()
    monkeypatch.setenv("PATH", str(root) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(tool, "_should_route_through_aux_vision", lambda: False)
    # A remote adapter must never call either controller acquisition/start path.
    from tools.bot_desktop import runtime
    from tools.computer_use import cua_backend_driver, cua_backend, cua_backend_capture
    from plugins.computer_use import cua as cua_provider
    from types import SimpleNamespace
    monkeypatch.setattr(cua_provider, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(cua_backend_capture, "sys", SimpleNamespace(platform="linux"))
    def wrong_target(*args, **kwargs):
        raise AssertionError("controller desktop/driver was consulted for a node")
    monkeypatch.setattr(runtime, "ensure_started_for_tool", wrong_target)
    monkeypatch.setattr(cua_backend_driver, "resolve_cua_driver_cmd", wrong_target)
    monkeypatch.setattr(cua_backend, "desktop_identity", wrong_target)
    monkeypatch.setattr(cua_backend_capture, "_linux_x11_active_window_id", wrong_target)
    tool.reset_backend_for_tests()
    import tools.computer_use_tool  # noqa: F401, installs the real public registration
    a, b = tmp_path / "profile-a", tmp_path / "profile-b"
    config(a)
    config(b, "book")

    def calls():
        log = root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    before = is_multiplex_active()
    set_multiplex_active(True)
    try:
        yield a, b, update, calls
    finally:
        tool.reset_backend_for_tests()
        set_multiplex_active(before)


def dispatch(action="capture", **args):
    from tools.registry import registry
    result = registry.dispatch("computer_use", {"action": action, **args}, session_id="same-session")
    return json.loads(result) if isinstance(result, str) else result


@pytest.mark.platforms("posix")
def test_default_explicit_node_image_capture_input_and_profile_interleaving(endpoint):
    a, b, update, calls = endpoint
    with home(a):
        assert tool.check_computer_use_requirements()
        first = dispatch()
        assert first.get("_multimodal"), first
        assert first["node"] == "studio"
        part = next(part for part in first["content"] if part["type"] == "image_url")
        assert part["image_url"]["url"].startswith("data:image/png;base64,")
        native = Image.open(io.BytesIO(base64.b64decode(part["image_url"]["url"].partition(",")[2])))
        assert native.size == (128, 80)  # The fixture returns actual PNG bytes, never a node-local path.
        assert "Node: studio" in first["text_summary"]
        assert dispatch("click", element=1)["ok"]
        other = dispatch(node="book")
        assert other["node"] == "book" and other.get("_multimodal")
        assert dispatch("type", node="book", text="book marker")["ok"]
        assert dispatch("click", element=1)["ok"]
    with home(b):
        assert dispatch()["node"] == "book"
    with home(a):
        assert dispatch("type", text="studio marker")["ok"]
        # Same frame on a different node/home must not suppress its first native image.
        assert tool.release_computer_use_session("same-session")
        assert not tool.release_computer_use_session("same-session")
    with home(b):
        assert tool.release_computer_use_session("same-session")
    events = calls()
    spawns = [event for event in events if event["name"] == "mcp_spawn"]
    assert len(spawns) == 3
    assert [Path(c["args"]["home"]) for c in spawns] == [a, a, b]
    assert all(c["args"].get("max_image_dimension") == 64 for c in events if c["name"] == "set_config")
    # The persistent transport/session which captured is the one which performs input.
    for event in events:
        if event["name"] in ("click", "type_text"):
            assert any(c["name"] == "get_window_state" and c["pid"] == event["pid"] for c in events)
            assert event["args"]["session"]
    studio = [x for x in events if x["name"] == "click" and x["host"] == "studio-alias"]
    assert len(studio) == 2 and studio[0]["pid"] == studio[1]["pid"]
    assert studio[0]["args"].get("element_token") == "fixture-token"
    assert studio[0]["args"].get("capture_id") == "capture-fixture"
    assert all("BatchMode=yes" in x["args"]["argv"] for x in spawns)
    assert all("/Applications/CuaDriver.app/Contents/MacOS/cua-driver mcp" == x["args"]["argv"][-1] for x in spawns)
    assert all(x["name"] != "mcp_spawn" or x["host"] in ("studio-alias", "book-alias") for x in events)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("state,code", [
    ({"locked": True}, "node_locked"),
    ({"permission_mode": "standard"}, "node_permission_mode_mismatch"),
    ({"daemon_running": False}, "node_daemon_absent"),
    ({"omit": ["status"]}, "node_state_unknown"),
    ({"capabilities": 1}, "node_asleep"),
    ({"accessibility": False}, "node_permission_denied"),
    ({"screen_recording": False}, "node_permission_denied"),
    ({"attribution": "ssh-terminal"}, "node_state_unknown"),
    ({"omit": ["console"]}, "node_state_unknown"),
    ({"omit": ["power"]}, "node_state_unknown"),
    ({"omit": ["version"]}, "node_state_unknown"),
    ({"offline": True}, "node_unreachable"),
    ({"denied": True}, "node_ssh_denied"),
])
def test_bad_node_never_creates_any_mcp_or_controller_backend(endpoint, state, code):
    a, _, update, calls = endpoint
    update(**{"book-alias": state})
    with home(a):
        result = dispatch(node="book")
    assert result["node"] == "book" and result["code"] == code
    assert result["ok"] is False
    assert not any(c["name"] == "mcp_spawn" for c in calls())
    assert {c["host"] for c in calls()} == {"book-alias"}


@pytest.mark.platforms("posix")
def test_mcp_autostart_mode_race_is_rechecked_before_any_desktop_operation(endpoint):
    a, _, update, calls = endpoint
    update(**{"book-alias": {"race_standard": True}})
    with home(a):
        result = dispatch(node="book")
    assert result["code"] == "node_permission_mode_mismatch"
    assert result["checks"]["permission_mode"]["value"] == "standard"
    assert any(c["name"] == "mcp_spawn" for c in calls())
    assert not any(c["name"] in ("start_session", "get_window_state", "click", "type_text") for c in calls())


@pytest.mark.platforms("posix")
def test_later_lock_change_is_observed_without_reusing_stale_backend(endpoint):
    a, _, update, calls = endpoint
    with home(a):
        assert dispatch().get("_multimodal")
        update(**{"studio-alias": {"locked": True}})
        assert dispatch("click", element=1)["code"] == "node_locked"
    assert not any(c["name"] == "click" for c in calls())


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure", [{"drop_write": True}, {"stall_write": True}])
def test_write_transport_loss_is_not_replayed_and_new_transport_requires_capture(endpoint, failure):
    a, _, update, calls = endpoint
    update(**{"studio-alias": failure})
    with home(a):
        assert dispatch().get("_multimodal")
        started = time.monotonic()
        result = dispatch("click", element=1)
        assert time.monotonic() - started < 12
        assert not result["ok"] and "unknown" in json.dumps(result)
        # A replacement transport has no target or element-token namespace.
        result = dispatch("click", element=1)
        assert not result["ok"]
        assert len([c for c in calls() if c["name"] == "click"]) == 1
        assert dispatch().get("_multimodal")  # identical frame must be redelivered after loss


@pytest.mark.platforms("posix")
def test_unknown_node_and_bad_shared_config_do_not_fall_back(endpoint):
    a, _, update, calls = endpoint
    with home(a):
        assert "Unknown node" in dispatch(node="typo")["error"]
        assert "nonempty" in dispatch(node="")["error"]
        (a / "config.yaml").write_text("nodes:\n  hosts: []\n")
        assert "mapping" in dispatch()["error"]
        (a / "config.yaml").write_text("nodes: [broken\n")
        assert "error" in dispatch()
        (a / "config.yaml").write_text("computer_use:\n  nodes: {}\n")
        assert "shared nodes.hosts" in dispatch()["error"]
    assert not calls()


@pytest.mark.platforms("posix")
def test_probe_deadline_is_bounded_and_unknown_is_not_missing_permission(endpoint):
    a, _, update, calls = endpoint
    update(**{"book-alias": {"stall_probe": True}})
    started = time.monotonic()
    with home(a):
        result = dispatch(node="book")
    assert time.monotonic() - started < 11
    assert result["code"] == "node_unreachable"
    assert all(row["value"] is None for row in result["checks"].values())


@pytest.mark.platforms("posix")
def test_handshake_deadline_cleans_up_ssh_child(endpoint):
    a, _, update, calls = endpoint
    update(**{"book-alias": {"stall_handshake": True}})
    started = time.monotonic()
    with home(a):
        result = dispatch(node="book")
    assert time.monotonic() - started < 12
    assert "error" in result and result["node"] == "book"
    pids = [c["pid"] for c in calls() if c["name"] == "mcp_spawn"]
    from tests.waiting import wait_until
    import psutil
    assert wait_until(lambda: all(not psutil.pid_exists(pid) for pid in pids), timeout=5)


@pytest.mark.platforms("posix")
def test_node_approval_remains_fail_closed_and_identifies_target(endpoint):
    a, _, update, calls = endpoint
    prompts = []
    def deny(command, description, **kwargs):
        prompts.append((command, description))
        return "deny"
    tool.set_approval_callback(deny)
    with home(a):
        result = dispatch("type", node="book", text="must not land")
    assert result["node"] == "book" and result.get("error")
    assert result["action"] == "type"
    assert prompts and "book" in str(prompts)
    assert not calls()  # Even the readiness probe waits for approval on mutating actions.


@pytest.mark.platforms("posix")
def test_slow_node_does_not_serialize_another_nodes_startup(endpoint):
    a, _, update, calls = endpoint
    update(**{"book-alias": {"stall_handshake": True}})
    from concurrent.futures import ThreadPoolExecutor
    from tests.waiting import wait_until
    def stalled():
        with home(a):
            return dispatch(node="book")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(stalled)
        wait_until(lambda: any(c["name"] == "mcp_spawn" and c["host"] == "book-alias" for c in calls()), timeout=5)
        started = time.monotonic()
        with home(a):
            result = dispatch(node="studio")
        assert result.get("_multimodal"), result
        assert time.monotonic() - started < 4
        assert pending.result(timeout=12)["node"] == "book"


def test_no_config_preserves_local_provider_and_explicit_local_is_not_a_magic_fallback(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("{}")
    seen = []
    def local_handler(args, **kwargs):
        seen.append(args)
        return json.dumps({"ok": True})
    from tools.computer_use.node_routing import handle_on_node
    with home(tmp_path):
        assert resolve_node() is None
        result = json.loads(handle_on_node({"action": "capture"}, local_handler))
        assert result == {"ok": True, "node": "local"}
        assert json.loads(handle_on_node({"node": "local"}, local_handler))["code"] == "node_unavailable"
    assert len(seen) == 1


def test_node_registry_is_shared_and_ssh_command_quotes_remote_path(tmp_path):
    config(tmp_path, "third", {"third": {"ssh": "user@third", "platform": "macos", "computer_use": {
        "driver": "/Applications/Driver With Space.app/Contents/MacOS/driver"}}})
    with home(tmp_path):
        nodes, default = load_nodes()
        assert default == "third" and resolve_node() == nodes["third"]
        argv = resolve_node().ssh_argv(resolve_node().cua_driver, "mcp")
        assert argv[-1] == "'/Applications/Driver With Space.app/Contents/MacOS/driver' mcp"
    config(tmp_path, "bad", {"bad": {"ssh": "-oProxyCommand=sh"}})
    with home(tmp_path), pytest.raises(NodeError):
        resolve_node()


@pytest.mark.platforms("posix")
def test_doctor_distinguishes_observed_false_true_and_unknown_and_cli_defaults(endpoint, capsys):
    a, _, update, calls = endpoint
    from argparse import Namespace
    from hermes_cli.subcommands.computer_use import _cu_doctor
    from tools.computer_use.node_doctor import parse_observations
    # Malformed/absent observations must not become false permission or true awake claims.
    checks = parse_observations({"version": "cua-driver 0.32.0"})
    assert checks["reachable"]["value"] is True
    assert checks["locked"]["value"] is checks["awake"]["value"] is None
    assert checks["version"]["value"] == "0.32.0"
    with home(a), pytest.raises(SystemExit) as exit:
        _cu_doctor(Namespace(node="book", json=True))
    assert exit.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["node"] == "book" and report["checks"]["locked"]["value"] is False
    assert report["checks"]["awake"]["value"] is True
    assert report["checks"]["daemon_running"]["value"] is True
    assert report["checks"]["permission_mode"]["value"] == "unrestricted"
    update(**{"studio-alias": {"omit": ["power"]}})
    with home(a), pytest.raises(SystemExit) as exit:
        _cu_doctor(Namespace(node=None, json=True))
    assert exit.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["node"] == "studio" and report["checks"]["awake"]["value"] is None
