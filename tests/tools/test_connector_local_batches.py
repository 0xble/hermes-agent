"""Local deferred tools retain the live agent path; batches must not bypass it."""

import json
import threading
from types import SimpleNamespace

import pytest


_TOOLSET = "batchfixture"


@pytest.fixture
def recorded_dispatch(monkeypatch):
    """Two real plugin-style deferrable tools that record each invocation."""
    from tools.connectors.gateway import bridge
    from tools.registry import invalidate_check_fn_cache, registry

    invoked = []
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    names = ("batch_alpha", "batch_beta")
    for name in names:
        registry.register(
            name, _TOOLSET, {"name": name, "description": name, "parameters": schema},
            lambda args, _name=name, **kw: (invoked.append((_name, args)) or json.dumps({"ran": _name, "n": args["n"]})))
    monkeypatch.setattr(bridge, "_default_client_factory", lambda call=None: invoked.append("gateway"))
    invalidate_check_fn_cache()
    try:
        yield invoked
    finally:
        for name in names:
            registry.deregister(name)
        invalidate_check_fn_cache()


def _entry(n, name="batch_alpha"):
    return {"name": name, "arguments": {"n": n}}


def test_direct_local_batch_runs_every_entry_in_order(recorded_dispatch):
    import model_tools

    calls = [_entry(3), _entry(1, "batch_beta"), _entry(2)]
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": calls}, tool_call_id="parent", enabled_toolsets=[_TOOLSET]))
    assert recorded_dispatch == [("batch_alpha", {"n": 3}), ("batch_beta", {"n": 1}), ("batch_alpha", {"n": 2})]
    assert [(item["index"], item["name"], item["response"]["n"]) for item in result["results"]] == [
        (0, "batch_alpha", 3), (1, "batch_beta", 1), (2, "batch_alpha", 2)]


def test_direct_local_batch_with_out_of_scope_entry_runs_nothing(recorded_dispatch):
    import model_tools

    calls = [_entry(1), {"name": "todo_list", "arguments": {}}]
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": calls}, enabled_toolsets=[_TOOLSET]))
    assert result["error"].startswith("calls[1]: 'todo_list' is not available in this session")
    assert recorded_dispatch == []


def test_direct_local_batch_naming_a_core_tool_says_call_it_directly(recorded_dispatch):
    """A core, directly-listed tool in a direct batch gets not_deferrable guidance (as the
    single-entry path does), not a tool_search round-trip, and nothing runs."""
    import model_tools
    from tools.tool_search import not_deferrable_error

    calls = [_entry(1), {"name": "terminal", "arguments": {"command": "echo hi"}}]
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": calls}, enabled_toolsets=[_TOOLSET]))
    assert result["error"] == f"calls[1]: {not_deferrable_error('terminal')}"
    assert "directly-listed tool" in result["error"] and "tool_search" not in result["error"]
    assert recorded_dispatch == []


def test_direct_local_batch_schema_error_names_the_failing_index(recorded_dispatch):
    import model_tools

    calls = [_entry(1), _entry(2, "batch_beta"), {"name": "batch_alpha", "arguments": {}}]
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": calls}, enabled_toolsets=[_TOOLSET]))
    assert result["error"].startswith("calls[2]: tool_call to 'batch_alpha' is missing required argument(s): n")
    assert result["constraint"] == "required"  # structured fields survive the prefix
    assert recorded_dispatch == []


def test_direct_local_batch_over_cap_runs_nothing_and_names_the_cap(recorded_dispatch):
    import model_tools
    from agent.tool_executor import _unwrap_tool_search_call
    from tools.connectors.gateway.config import MAX_CALLS_PER_DISPATCH

    calls = [_entry(n) for n in range(MAX_CALLS_PER_DISPATCH + 1)]
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": calls}, enabled_toolsets=[_TOOLSET]))
    assert result["error"].startswith(f"too many calls: 11 > max {MAX_CALLS_PER_DISPATCH}")
    assert recorded_dispatch == []

    agent = SimpleNamespace(enabled_toolsets=[_TOOLSET], disabled_toolsets=[])
    name, args, block = _unwrap_tool_search_call(agent, "tool_call", {"calls": calls})
    assert (name, args) == ("tool_call", {"calls": calls})  # sentinel never leaks downstream
    assert block is not None and block.startswith("too many calls: 11")

    direct = json.loads(model_tools._dispatch_local_batch(
        calls, model_tools._CallIds(), user_task=None, enabled_tools=None, middleware_trace=[],
        enabled_toolsets=[_TOOLSET], disabled_toolsets=None))
    assert direct["error"].startswith("too many calls: 11") and recorded_dispatch == []


def test_direct_local_batch_stops_unstarted_entries_on_interrupt(recorded_dispatch, monkeypatch):
    import model_tools
    from tools import interrupt

    checks = iter([False, True, True])
    monkeypatch.setattr(interrupt, "is_interrupted", lambda: next(checks))
    result = json.loads(model_tools._dispatch_local_batch(
        [_entry(0), _entry(1), _entry(2)], model_tools._CallIds(tool_call_id="parent"),
        user_task=None, enabled_tools=None, middleware_trace=[], enabled_toolsets=[_TOOLSET],
        disabled_toolsets=None))
    assert recorded_dispatch == [("batch_alpha", {"n": 0})]
    assert [item.get("error", {}).get("code") for item in result["results"]] == [None, "INTERRUPTED", "INTERRUPTED"]
    assert [item["index"] for item in result["results"]] == [0, 1, 2]


def test_direct_local_batch_runs_per_entry_hooks_even_when_wrapper_skipped(recorded_dispatch, monkeypatch):
    """The wrapper's skip flags must not leak to entries: each runs its own pre_tool_call
    hook and execution middleware, under a unique child id even without a parent id."""
    import model_tools
    from hermes_cli import middleware, plugins

    hooked, executed = [], []

    def pre_hook(name, args, **kw):
        hooked.append((name, kw["tool_call_id"]))
        return None, None

    def run_mw(name, args, dispatch, **kw):
        executed.append(name)
        return dispatch(args)

    monkeypatch.setattr(plugins, "_dispatch_pre_tool_call_hooks", pre_hook)
    monkeypatch.setattr(middleware, "run_tool_execution_middleware", run_mw)
    calls = [_entry(1), _entry(2, "batch_beta")]
    runs = []
    for _ in range(2):
        hooked.clear()
        json.loads(model_tools.handle_function_call(
            "tool_call", {"calls": calls}, enabled_toolsets=[_TOOLSET], skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True, skip_tool_execution_middleware=True))
        assert [name for name, _ in hooked] == ["batch_alpha", "batch_beta"]
        assert all(cid and not cid.startswith("None") for _, cid in hooked)
        runs.append([cid for _, cid in hooked])
    assert executed == ["batch_alpha", "batch_beta"] * 2
    assert len(set(runs[0] + runs[1])) == 4  # no collisions across parentless batches


def test_direct_mixed_local_and_connector_batch_names_the_split(recorded_dispatch):
    import model_tools

    calls = [_entry(1), {"name": "connectors__gmail__SEND_EMAIL", "arguments": {}}]
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": calls}, enabled_toolsets=[_TOOLSET]))
    assert "split local and connector tools into separate tool_call batches" in result["error"]
    assert "not available" not in result["error"] and "tool_search" not in result["error"]
    assert recorded_dispatch == []


def test_direct_local_batch_normalizes_entry_errors(recorded_dispatch, monkeypatch):
    import model_tools
    from tools.registry import registry

    real = registry.dispatch
    monkeypatch.setattr(registry, "dispatch", lambda name, args, **kw: (
        json.dumps({"error": "boom"}) if name == "batch_beta" else real(name, args, **kw)))
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": [_entry(1), _entry(2, "batch_beta")]}, tool_call_id="p",
        enabled_toolsets=[_TOOLSET]))
    assert result["results"][1]["error"] == {"code": "TOOL_ERROR", "message": "boom"}
    assert result["results"][0]["response"]["n"] == 1


def test_direct_local_batch_passes_successful_null_error_through_unchanged(recorded_dispatch, monkeypatch):
    import model_tools
    from tools.registry import registry

    ok = {"success": True, "error": None, "data": 1}
    real = registry.dispatch
    monkeypatch.setattr(registry, "dispatch", lambda name, args, **kw: (
        json.dumps(ok) if name == "batch_beta" else real(name, args, **kw)))
    result = json.loads(model_tools.handle_function_call(
        "tool_call", {"calls": [_entry(1), _entry(2, "batch_beta")]}, tool_call_id="p",
        enabled_toolsets=[_TOOLSET]))
    assert result["results"][1] == {"index": 1, "name": "batch_beta", "response": ok}
    assert result["results"][0]["response"]["n"] == 1


@pytest.mark.parametrize(("value", "expected"), [
    ({"success": True, "error": None, "data": 1}, None),
    ({"error": None}, None),
    ({"error": ""}, None),
    ({"error": False}, None),
    ({"error": {}}, None),
    ({"error": []}, None),
    ({"success": True, "error": "warned"}, None),
    ({"error": "boom"}, {"code": "TOOL_ERROR", "message": "boom"}),
    ({"success": False, "error": {"code": "X", "message": "m"}}, {"code": "X", "message": "m"}),
    ({"error": {"detail": 1}}, {"code": "TOOL_ERROR", "message": '{"detail": 1}'}),
    ("plain", None),
])
def test_batch_entry_error_is_the_shared_failure_predicate(value, expected):
    from tools.connectors.gateway.merge import batch_entry_error

    assert batch_entry_error(value) == expected


def test_dispatch_connector_batch_passes_successful_null_error_through(monkeypatch):
    import model_tools
    from tools.connectors.dispatch import dispatch_connector_batch

    ok = {"success": True, "error": None, "data": 1}
    monkeypatch.setattr(model_tools, "handle_function_call", lambda *a, **kw: json.dumps(ok))
    result = json.loads(dispatch_connector_batch(
        [{"name": "connectors__gmail__SEND_EMAIL", "arguments": {}}], model_tools._CallIds(),
        user_task=None, enabled_tools=None, middleware_trace=[], enabled_toolsets=None,
        disabled_toolsets=None))
    assert result["results"][0]["response"] == ok and "error" not in result["results"][0]


@pytest.mark.parametrize("flatten_probe", [False, True])
def test_single_local_unwrap_keeps_session_db_todo_store_and_setup_callback(tmp_path, flatten_probe):
    from agent.tool_executor import _unwrap_tool_search_call
    from agent.agent_runtime_helpers import invoke_tool
    from hermes_state import SessionDB
    from tools.connectors import live
    from tools.connectors.contract import SettleReason
    from tools.connectors.mcp import apply_answer
    from tools.todo_tool import TodoStore

    from gateway.session_context import reset_session_vars, set_session_vars

    # A desktop session: the MCP card exists only there.
    set_session_vars(source="desktop", session_key="current-session", session_id="current-session")

    db = SessionDB(tmp_path / "recall.db")
    db.create_session("past-session", source="cli")
    db.append_message("past-session", role="user", content="live-db-proof")
    callbacks = []
    def connection(payload):
        callbacks.append(payload)

        def respond():
            operation = live.get("current-session", payload["op_id"])
            if operation is not None:
                apply_answer(operation, json.dumps(
                    {"targets": [{"name": t["name"], "status": "skipped"} for t in payload["targets"]]}))
                operation.settle(SettleReason.all_resolved)

        threading.Timer(0.02, respond).start()

    agent = SimpleNamespace(
        enabled_toolsets=["todo", "session_search", "connections"], disabled_toolsets=[],
        session_id="current-session", _todo_store=TodoStore(), _memory_manager=None,
        _get_session_db_for_recall=lambda: db, connection_callback=connection,
    )
    calls = [
        {"name": "session_search", "arguments": {"session_id": "past-session"}},
        {"name": "todo_list", "arguments": {"todos": [{"id": "a", "content": "live-store-proof", "status": "pending"}]}},
        {"name": "manage_connections", "arguments": {
            "action": "install", "connectors": [{"name": "linear", "mcp": True}]}},
    ]
    results = []
    try:
        for entry in calls:
            if entry["name"] == "manage_connections":
                # Not deferrable, so it reaches invoke_tool directly and must find the agent callback.
                name, args, error = entry["name"], entry["arguments"], None
            else:
                name, args, error = _unwrap_tool_search_call(
                    agent, "tool_call", {"calls": [entry]}, flatten_probe=flatten_probe)
            assert name == entry["name"] and error is None
            results.append(json.loads(invoke_tool(
                agent, name, args, "task", tool_call_id="call", pre_tool_block_checked=True)))
        assert "live-db-proof" in json.dumps(results[0])
        assert agent._todo_store.read()[0]["content"] == "live-store-proof"
        assert results[2]["targets"][0] == {
            "name": "linear", "kind": "mcp", "action": "install", "state": "skipped"}
        assert [(c["tool_call_id"], [t["name"] for t in c["targets"]]) for c in callbacks] == [("call", ["linear"])]
    finally:
        db.close()
        reset_session_vars()


def test_dispatch_connector_batch_guard_echoes_first_entry(monkeypatch):
    """Direct call: the dispatcher's own local-entry guard (unreachable via tool_call, which
    partitions first) must reject with the echoed retry shape before any entry runs."""
    import model_tools
    from tools.connectors.dispatch import dispatch_connector_batch

    invoked = []
    monkeypatch.setattr(model_tools, "handle_function_call", lambda *a, **kw: invoked.append(a))
    calls = [{"name": "session_search", "arguments": {"query": "alpha"}},
             {"name": "connectors__gmail__SEND_EMAIL", "arguments": {}}]
    result = json.loads(dispatch_connector_batch(
        calls, model_tools._CallIds(), user_task=None, enabled_tools=None,
        middleware_trace=[], enabled_toolsets=None, disabled_toolsets=None))
    assert 'Retry with only: {"calls":[{"name":"session_search","arguments":{"query":"alpha"}}]}' in result["error"]
    assert invoked == []
