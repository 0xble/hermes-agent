"""Archive the producing invocation after node binding ends, including batch spill."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.agent.test_run_agent import agent  # noqa: F401
from tests.tools.test_named_node_execution import call, peers  # noqa: F401
from tools.budget_config import BudgetConfig
from tools.tool_result_storage import extract_persisted_path

pytestmark = pytest.mark.platforms("posix")


def tool_call(name, args, key):
    return SimpleNamespace(id=key, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def run_batch(agent, calls, route):
    from agent import tool_executor as executor
    message = SimpleNamespace(tool_calls=calls)
    rows = []
    if route == "segmented":
        executor.execute_tool_calls_segmented(agent, message, rows, "archive-owner",
            segments=[("parallel", calls[:1]), ("sequential", calls[1:])])
    else:
        getattr(executor, "execute_tool_calls_" + route)(agent, message, rows, "archive-owner")
    return rows


def budget(mode):
    return BudgetConfig(default_result_size=2_000 if mode == "result" else 50_000,
                        turn_budget=100_000 if mode == "result" else 3_000, preview_size=200)


@pytest.fixture
def archive_transport(peers, monkeypatch):
    # These peers have separate namespaces, not the controller's spillover mount.
    monkeypatch.setattr("tools.tool_result_storage._sandbox_visible_spillover_path", lambda *args: None)
    from tools.environments.ssh import SSHEnvironment

    roots = peers[0]
    initialize = SSHEnvironment.__init__

    def initialize_peer(env, host, *args, **kwargs):
        # BaseEnvironment asks for its temp directory before SSH assigns host.
        env._fixture_temp_dir = str(roots[host] / "tmp")
        initialize(env, host, *args, **kwargs)

    monkeypatch.setattr(SSHEnvironment, "__init__", initialize_peer)
    monkeypatch.setattr(SSHEnvironment, "get_temp_dir", lambda env: env._fixture_temp_dir)
    return peers


def command(label):
    return "python3 -c 'print(\"" + label + "-archive-line\\n\" * 500)'"


def read_archive(row, node, label):
    path = extract_persisted_path(row["content"])
    assert path, row
    assert f"/{node}/tmp/hermes-results/" in path, "archive was not written to the producing fixture node"
    assert f"node={node!r}" in row["content"], row
    result = call("read_file", {"node": node, "path": path})
    assert label + "-archive-line" in result.get("content", ""), result
    archived = Path(path).read_text()
    assert json.loads(archived)["node"] == node
    assert json.loads(archived)["output"].count(label + "-archive-line") == 500
    return path


@pytest.mark.parametrize("route", ["sequential", "concurrent", "segmented"])
@pytest.mark.parametrize("mode", ["result", "aggregate"])
@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "default"])
def test_producing_node_survives_dispatch_return_and_default_change(archive_transport, agent, monkeypatch, route, mode, explicit):
    import model_tools

    *_, home = archive_transport
    dispatch = model_tools.handle_function_call

    def dispatch_then_change_default(name, args, *positional, **kwargs):
        result = dispatch(name, args, *positional, **kwargs)
        config = json.loads((home / "config.yaml").read_text())
        config["nodes"]["default"] = "macbook"
        (home / "config.yaml").write_text(json.dumps(config))
        return result

    args = {"command": command("studio")}
    if explicit:
        args["node"] = "macstudio"
    with patch("model_tools.handle_function_call", side_effect=dispatch_then_change_default), patch(
        "agent.tool_executor._budget_for_agent", return_value=budget(mode)
    ):
        rows = run_batch(agent, [tool_call("terminal", args, "producing-node")], route)
    assert len(rows) == 1
    read_archive(rows[0], "macstudio", "studio")
    assert not any(key.startswith("_result") for key in rows[0]), "environment objects escaped to transcript"


@pytest.mark.parametrize("route", ["concurrent", "segmented"])
@pytest.mark.parametrize("mode", ["result", "aggregate"])
def test_mixed_batch_keeps_each_result_on_its_node(archive_transport, agent, route, mode):
    import model_tools
    dispatch = model_tools.handle_function_call
    calls = [tool_call("terminal", {"node": node, "command": command(node)}, "mixed-" + node)
             for node in ("macstudio", "macbook")]
    calls.append(tool_call("web_search", {}, "unrelated-controller"))

    def mixed_dispatch(name, args, *positional, **kwargs):
        return "controller-only\n" * 500 if name == "web_search" else dispatch(name, args, *positional, **kwargs)

    with patch("agent.tool_executor._budget_for_agent", return_value=budget(mode)), patch(
        "model_tools.handle_function_call", side_effect=mixed_dispatch
    ):
        rows = run_batch(agent, calls, route)
    assert [row["tool_call_id"] for row in rows] == [call.id for call in calls]
    paths = [read_archive(row, node, node) for row, node in zip(rows, ("macstudio", "macbook"))]
    assert paths[0] != paths[1]
    local_path = extract_persisted_path(rows[2]["content"])
    raw_controller = "controller-only\n" * 500
    # Aggregate enforcement archives the canonical, already-delimited row.
    # Per-result enforcement archives raw output before message construction.
    from agent.tool_dispatch_helpers import make_tool_result_message
    expected_controller = (make_tool_result_message("web_search", raw_controller, "unrelated-controller")["content"]
                           if mode == "aggregate" else raw_controller)
    assert local_path and Path(local_path).read_text() == expected_controller, rows[2]
    assert "/macstudio/" not in local_path and "/macbook/" not in local_path
    assert "node=" not in rows[2]["content"]


def test_remote_archive_failure_does_not_publish_controller_path(archive_transport, agent):
    import model_tools

    _, _, offline, *_ = archive_transport
    dispatch = model_tools.handle_function_call

    def disconnect_after_result(name, args, *positional, **kwargs):
        result = dispatch(name, args, *positional, **kwargs)
        offline.add("macstudio")
        return result

    try:
        with patch("model_tools.handle_function_call", side_effect=disconnect_after_result), patch(
            "agent.tool_executor._budget_for_agent", return_value=budget("result")
        ):
            rows = run_batch(agent, [tool_call("terminal", {"node": "macstudio", "command": command("studio")}, "offline")], "sequential")
        assert extract_persisted_path(rows[0]["content"]) is None, rows
        assert "Full output could not be saved" in rows[0]["content"], rows
    finally:
        offline.clear()


def test_process_handle_uses_original_environment_after_registry_default_changes(archive_transport, agent):
    from tools import process_registry as pr

    *_, registry, home = archive_transport
    spawned = call("terminal", {"node": "macstudio", "command": command("process"), "background": True})
    handle = spawned["session_id"]
    session = registry._lookup_session(handle)
    # Drop the in-memory environment exactly as recovery does. The handle retains connection identity.
    session.env_ref = None
    config = json.loads((home / "config.yaml").read_text())
    config["nodes"]["default"] = "macbook"
    (home / "config.yaml").write_text(json.dumps(config))
    with patch("agent.tool_executor._budget_for_agent", return_value=budget("result")):
        rows = run_batch(agent, [tool_call("process_manage", {"action": "wait", "session_id": handle, "timeout": 15}, "process-original")], "sequential")
    path = extract_persisted_path(rows[0]["content"])
    assert path and "node='macstudio'" in rows[0]["content"], rows
    assert "process-archive-line" in call("read_file", {"node": "macstudio", "path": path}).get("content", "")

@pytest.mark.parametrize("route", ["sequential", "concurrent", "segmented"])
@pytest.mark.parametrize("mode", ["result", "aggregate"])
def test_multimodal_archive_keeps_images_and_producing_node(archive_transport, agent, monkeypatch, route, mode):
    import model_tools

    *_, home = archive_transport
    dispatch = model_tools.handle_function_call
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}
    produced = []
    monkeypatch.setattr(agent, "_model_supports_vision", lambda: True)
    monkeypatch.setattr(agent, "_provider_supports_vision_tool_messages", lambda: True)

    def multimodal(name, args, *positional, **kwargs):
        result = json.loads(dispatch(name, args, *positional, **kwargs))
        assert result["exit_code"] == 0 and result["node"] == "macstudio", result
        produced.append(result["output"])
        config = json.loads((home / "config.yaml").read_text())
        config["nodes"]["default"] = "macbook"
        (home / "config.yaml").write_text(json.dumps(config))
        return {"_multimodal": True, "content": [{"type": "text", "text": result["output"]}, image],
                "text_summary": result["output"]}

    with patch("model_tools.handle_function_call", side_effect=multimodal), patch(
        "agent.tool_executor._budget_for_agent", return_value=budget(mode)
    ):
        rows = run_batch(agent, [tool_call("terminal", {"command": command("multimodal")}, "multimodal-" + mode)], route)
    parts = rows[0]["content"]
    assert isinstance(parts, list) and parts[1] == image, rows
    text = parts[0]["text"]
    path = extract_persisted_path(text)
    assert path and "/macstudio/tmp/hermes-results/" in path and "node='macstudio'" in text, rows
    recovered = call("read_file", {"node": "macstudio", "path": path})
    assert "multimodal-archive-line" in recovered.get("content", ""), recovered
    assert Path(path).read_text() == produced[0]
    assert produced[0].count("multimodal-archive-line") == 500


@pytest.mark.parametrize("mode", ["result", "aggregate"])
def test_archive_destination_does_not_leak_between_sessions(archive_transport, agent, mode):
    import model_tools
    from tools.approval_context import set_current_session_key, reset_current_session_key

    *_, home = archive_transport
    dispatch = model_tools.handle_function_call
    rows = []

    def change_default(name, args, *positional, **kwargs):
        result = dispatch(name, args, *positional, **kwargs)
        config = json.loads((home / "config.yaml").read_text())
        config["nodes"]["default"] = "macbook" if config["nodes"]["default"] == "macstudio" else "macstudio"
        (home / "config.yaml").write_text(json.dumps(config))
        return result

    with patch("model_tools.handle_function_call", side_effect=change_default), patch(
        "agent.tool_executor._budget_for_agent", return_value=budget(mode)
    ):
        for session, node in (("archive-chat-a", "macstudio"), ("archive-chat-b", "macbook")):
            token = set_current_session_key(session)
            try:
                rows.extend(run_batch(agent, [tool_call("terminal", {"command": command(node)}, session)], "sequential"))
            finally:
                reset_current_session_key(token)
    paths = [read_archive(row, node, node) for row, node in zip(rows, ("macstudio", "macbook"))]
    assert paths[0] != paths[1]
    for node, path in zip(("macstudio", "macbook"), paths):
        assert json.loads(Path(path).read_text())["output"].count(node + "-archive-line") == 500


def test_legacy_controller_archive_remains_controller_owned(archive_transport, agent):
    *_, home = archive_transport
    (home / "config.yaml").write_text(json.dumps({"nodes": {"hosts": {}}}))
    with patch("model_tools.handle_function_call", return_value="legacy-controller\n" * 500), patch(
        "agent.tool_executor._budget_for_agent", return_value=budget("result")
    ):
        rows = run_batch(agent, [tool_call("web_search", {}, "controller")], "sequential")
    path = extract_persisted_path(rows[0]["content"])
    assert path and Path(path).read_text() == "legacy-controller\n" * 500
    assert "node=" not in rows[0]["content"]
