"""Generated stubs and the production file poller over real shell/filesystem I/O."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from tools.code_execution_tool import generate_hermes_tools_module
from tools.code_execution_rpc import _rpc_poll_loop


@pytest.mark.parametrize("transport", ["uds", "file"])
def test_generated_tool_errors_raise_and_block_empty_fallback(transport):
    """A failed read must stop read-modify-write code before it can write empty text."""
    namespace = {}
    exec(generate_hermes_tools_module(["read_file", "write_file"], transport=transport), namespace)
    writes = []
    fail_existing = False

    def dispatch(name, args):
        if name == "read_file" and (args["path"] == "missing" or fail_existing):
            result = {"error": f"File not found: {args['path']}"}
        elif name == "read_file":
            result = {"content": "original\n", "total_lines": 1}
        else:
            writes.append(args)
            result = {"status": "success"}
        return namespace["_parse_tool_result"](name, result)

    namespace["_call"] = dispatch
    assert namespace["read_file"]("existing")["content"] == "original\n"
    with pytest.raises(namespace["HermesToolError"], match="read_file failed: File not found: missing"):
        namespace["read_file"]("missing")
    fail_existing = True
    with pytest.raises(namespace["HermesToolError"], match="File not found: existing"):
        content = namespace["read_file"]("existing").get("content", "")
        namespace["write_file"]("existing", content)
    assert writes == []
    with pytest.raises(namespace["HermesToolError"], match="content was not returned"):
        namespace["_parse_tool_result"]("read_file", {"status": "unchanged", "content_returned": False})


@pytest.mark.parametrize("transport", ["uds", "file"])
def test_retry_does_not_retry_hermes_tool_error(transport):
    namespace = {}
    exec(generate_hermes_tools_module(["read_file"], transport=transport), namespace)
    attempts = []

    def fail():
        attempts.append(None)
        raise namespace["HermesToolError"]("read_file", "File not found: missing")

    with pytest.raises(namespace["HermesToolError"], match="read_file failed: File not found: missing"):
        namespace["retry"](fail, max_attempts=3, delay=0)
    assert len(attempts) == 1


@pytest.mark.parametrize("transport", ["uds", "file"])
def test_non_file_tool_error_envelope_is_returned(transport):
    namespace = {}
    exec(generate_hermes_tools_module(["terminal"], transport=transport), namespace)
    namespace["_call"] = lambda name, args: namespace["_parse_tool_result"](
        name, {"error": "command failed", "exit_code": 124}
    )

    assert namespace["terminal"]("sleep 10", timeout=1) == {"error": "command failed", "exit_code": 124}


CALLS = {
    "web_search": {"query": "fixture", "limit": 3},
    "web_extract": {"urls": ["https://example.test"], "char_limit": 3000},
    "read_file": {"path": "reference", "offset": 2, "limit": 4, "node": "fixture-node"},
    "write_file": {"path": "output", "content": "café", "cross_profile": False, "node": "fixture-node"},
    "search_files": {"pattern": "x", "target": "files", "path": ".", "file_glob": "*.py",
                     "limit": 4, "offset": 2, "output_mode": "count", "context": 3, "order": "modified", "node": "fixture-node"},
    "patch": {"path": "output", "old_string": "old", "new_string": "new", "replace_all": True,
              "mode": "replace", "patch": None, "cross_profile": False, "node": "fixture-node"},
    "terminal": {"command": "echo fixture", "timeout": 3, "workdir": "/tmp", "node": "fixture-node"},
}


@pytest.mark.parametrize("transport", ["uds", "file"])
@pytest.mark.parametrize("name", ["read_file", "write_file", "search_files", "patch", "terminal"])
def test_generated_node_omission_preserves_legacy_payload(transport, name):
    namespace = {}
    exec(generate_hermes_tools_module([name], transport=transport), namespace)
    calls = []
    namespace["_call"] = lambda tool, args: calls.append((tool, args)) or {"status": "success"}
    legacy = {key: value for key, value in CALLS[name].items() if key != "node"}

    assert namespace[name](**legacy) == {"status": "success"}
    assert calls == [(name, legacy)]


@pytest.mark.platforms("posix")
def test_generated_file_rpc_kwargs_correlation_and_authority(tmp_path, monkeypatch):
    from pm.shell import bash
    from tools.registry import registry
    import tools.file_tools
    import tools.web_tools
    import tools.terminal_tool

    shell = bash()
    assert shell
    rpc = tmp_path / "rpc with spaces"
    rpc.mkdir()
    monkeypatch.delenv("HERMES_RPC_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    namespace = {}
    exec(generate_hermes_tools_module([], transport="file"), namespace)
    assert namespace["_RPC_DIR"] == str(tmp_path / "hermes_rpc")
    assert "terminal" not in namespace
    monkeypatch.setenv("HERMES_RPC_DIR", str(rpc))
    monkeypatch.setenv("HERMES_RPC_TOKEN", "right-token")
    exec(generate_hermes_tools_module(list(CALLS), transport="file"), namespace)
    seen, log, counter = [], [], [0]

    def dispatch(name, args, **kwargs):
        seen.append((name, args.copy()))
        return json.dumps({"name": name, "args": args})

    monkeypatch.setattr("model_tools.handle_function_call", dispatch)

    class Shell:
        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            result = subprocess.run([shell, "-c", command], cwd=cwd, timeout=timeout,
                                    env=dict(os.environ), input=stdin_data or "", capture_output=True, text=True, check=False)
            assert result.returncode == 0, result.stderr
            return {"output": result.stdout}

    stop = threading.Event()
    budget = len(CALLS) + 8
    poller = threading.Thread(target=_rpc_poll_loop, args=(Shell(), str(rpc), "owner", log, counter,
                               budget, frozenset(CALLS), stop, "right-token"), daemon=True)
    poller.start()
    try:
        # Raw clients bypass stub visibility; neither missing nor wrong token may dispatch.
        for seq, token in [(9001, None), (9002, "wrong-token")]:
            request = {"seq": seq, "tool": "terminal", "args": {"command": "forbidden"}}
            if token is not None:
                request["token"] = token
            path = rpc / f"req_{seq}"
            path.write_text(json.dumps(request), encoding="utf-8")
            deadline = time.monotonic() + 5
            while path.exists() and time.monotonic() < deadline:
                stop.wait(.01)
            assert not path.exists()
            assert not (rpc / f"res_{seq:06d}").exists()
        assert seen == [] and counter == [0]
        for name, args in CALLS.items():
            blocked = {"background", "heartbeat", "pty", "notify", "notify_on_complete", "watch_patterns", "persist_on_release", "survive_restart"} if name == "terminal" else set()
            schema_keys = set(registry.get_entry(name).schema["parameters"]["properties"]) - blocked
            assert schema_keys <= set(args), (name, schema_keys - set(args))
            assert namespace[name](**args) == {"name": name, "args": args}
        assert seen == list(CALLS.items())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: namespace["terminal"](f"tag-{i}", 3, "/tmp"), range(8)))
        assert [r["args"]["command"] for r in results] == [f"tag-{i}" for i in range(8)]
        assert all("node" not in result["args"] for result in results)
        assert "not available in execute_code" in namespace["_call"]("unauthorized-tool", {})["error"]
        assert "limit reached" in namespace["terminal"]("over-budget")["error"]
        assert counter == [budget] and len(seen) == len(log) == budget
    finally:
        stop.set()
        poller.join(timeout=10)
        assert not poller.is_alive()
