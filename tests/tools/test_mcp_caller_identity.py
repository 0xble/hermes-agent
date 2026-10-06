"""MCP servers opted into ``caller_identity`` receive the calling session as request ``_meta``.

A long-lived MCP server shared by every gateway session inherits the gateway's ``os.environ``,
whose ``HERMES_SESSION_ID`` is last-writer-wins. The identity must come from the calling turn's
ContextVars, and the model's arguments must never reach request ``_meta``.
"""

import asyncio
import json
import logging
import sqlite3
import sys
import textwrap
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import session_context
from tools import mcp_tool, mcp_tool_discovery, mcp_tool_handlers, mcp_tool_loop

CALLER = "hermes/caller"


def _result(text="ok"):
    return SimpleNamespace(content=[SimpleNamespace(text=text, type="text")], isError=False,
                           structuredContent=None)


def _fake_server():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_result())
    return SimpleNamespace(session=session, _rpc_lock=asyncio.Lock())


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(session_context, "_session_context_engaged", False)
    session_context.reset_session_vars()
    yield tmp_path
    session_context.reset_session_vars()


@pytest.fixture
def servers(home):
    """``relay`` opted in, ``other`` not; calls run on the REAL MCP loop thread."""
    relay, other = _fake_server(), _fake_server()
    mcp_tool_loop._ensure_mcp_loop()
    try:
        with patch.dict(mcp_tool._servers, {"relay": relay, "other": other}), \
             patch.object(mcp_tool, "_caller_identity_servers", {"relay"}), \
             patch.dict(mcp_tool._server_error_counts, {}, clear=True):
            yield relay.session.call_tool, other.session.call_tool
    finally:
        mcp_tool_loop._stop_mcp_loop()


def _bind(session_id, profile="work"):
    return session_context.set_session_vars(platform="telegram", session_id=session_id, profile=profile)


def _sessions_db(home, rows):
    """state.db with the real SessionDB schema and ``(id, source, parent)`` rows."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    for session_id, source, parent in rows:
        db.create_session(session_id=session_id, source=source, parent_session_id=parent)
    db.close()


def _meta_of(call_tool):
    return call_tool.await_args.kwargs.get("meta")


def test_only_opted_in_server_receives_identity(servers, tmp_path):
    relay, other = servers
    _sessions_db(tmp_path, [("topic-1", "telegram", None)])
    _bind("topic-1")
    spoof = {CALLER: {"session_id": "someone-else"}}

    mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)({"body": "hi", "_meta": spoof})
    mcp_tool_handlers._make_tool_handler("other", "send", 30.0)({"body": "hi", "_meta": spoof})

    assert relay.await_args.kwargs == {
        "arguments": {"body": "hi", "_meta": spoof},
        "meta": {CALLER: {"profile": "work", "session_id": "topic-1", "topic_session_id": "topic-1"}},
    }
    # A server that did not opt in gets exactly today's request: no meta keyword at all.
    assert other.await_args.kwargs == {"arguments": {"body": "hi", "_meta": spoof}}


def test_concurrent_sessions_each_send_their_own_identity(servers, monkeypatch, tmp_path):
    """Two turns in different contexts on one shared server, os.environ holding a third session:
    each call carries its own turn's ContextVar id, never the process-wide mirror."""
    relay, _ = servers
    _sessions_db(tmp_path, [("A", "telegram", None), ("B", "discord", None)])
    monkeypatch.setenv("HERMES_SESSION_ID", "env-third")
    both_bound = threading.Barrier(2)
    seen = {}

    async def _record(name, arguments, **kwargs):
        seen[arguments["who"]] = kwargs["meta"][CALLER]
        await asyncio.sleep(0.05)
        return _result()
    relay.side_effect = _record

    def _turn(session_id):
        _bind(session_id)
        both_bound.wait(timeout=5)
        mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)({"who": session_id})

    threads = [threading.Thread(target=_turn, args=(sid,)) for sid in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert {who: (meta["session_id"], meta["topic_session_id"]) for who, meta in seen.items()} == {
        "A": ("A", "A"), "B": ("B", "B")}


def test_delegated_child_maps_to_nearest_non_subagent_ancestor(servers, tmp_path):
    relay, _ = servers
    # topic-2 continues compressed topic-1; child/grandchild are delegated subagents under topic-2.
    _sessions_db(tmp_path, [
        ("topic-1", "telegram", None), ("topic-2", "telegram", "topic-1"),
        ("child", "subagent", "topic-2"), ("grandchild", "subagent", "child"),
    ])
    _bind("grandchild")

    mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)({})

    assert _meta_of(relay)[CALLER] == {"profile": "work", "session_id": "grandchild",
                                       "topic_session_id": "topic-2"}


@pytest.mark.parametrize("rows", [
    pytest.param([("child", "subagent", "missing-parent")], id="missing-ancestor"),
    pytest.param([("child", "subagent", "loop"), ("loop", "subagent", "child")], id="cycle"),
    pytest.param(None, id="no-state-db"),
])
def test_unresolvable_topic_is_null(servers, tmp_path, rows):
    relay, _ = servers
    if rows is not None:
        _sessions_db(tmp_path, [(sid, source, None) for sid, source, _ in rows])
        with sqlite3.connect(tmp_path / "state.db") as conn:  # SessionDB will not write a dangling parent or a cycle
            conn.executemany("UPDATE sessions SET parent_session_id = ? WHERE id = ?",
                             [(parent, sid) for sid, _, parent in rows])
    _bind("child")

    mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)({})

    assert _meta_of(relay)[CALLER] == {"profile": "work", "session_id": "child", "topic_session_id": None}


def test_no_bound_session_sends_no_meta(servers, monkeypatch):
    """Never invented: an engaged process whose calling context has no session ignores the
    os.environ mirror (another turn's id), and a plain CLI without a session id sends nothing."""
    relay, _ = servers
    handler = mcp_tool_handlers._make_tool_handler("relay", "send", 30.0)

    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    handler({})
    assert relay.await_args.kwargs == {"arguments": {}}

    monkeypatch.setenv("HERMES_SESSION_ID", "other-turn")
    monkeypatch.setattr(session_context, "_session_context_engaged", True)
    handler({})
    assert relay.await_args.kwargs == {"arguments": {}}


@pytest.mark.parametrize("value,opted_in", [
    (True, True), ("true", True), (False, False), (None, False), ("sometimes", False), ("absent", False),
])
def test_caller_identity_config_is_validated_like_other_booleans(monkeypatch, caplog, value, opted_in):
    monkeypatch.setattr(mcp_tool_discovery, "_connect_cooldown_active", lambda name: False)
    config = {"command": "relay"} if value == "absent" else {"command": "relay", "caller_identity": value}
    with patch.object(mcp_tool, "_caller_identity_servers", {"relay"} if not opted_in else set()), \
         patch.object(mcp_tool, "_server_connecting", set()), \
         patch.dict(mcp_tool._server_scope_keys, {}), \
         patch.dict(mcp_tool._server_connect_errors, {}), \
         patch.object(mcp_tool, "_parallel_safe_servers", set()), \
         caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        mcp_tool_discovery._select_new_servers({"relay": config})
        assert mcp_tool_discovery.mcp_server_wants_caller_identity("relay") is opted_in
    assert ("boolean-ish" in caplog.text) is (value == "sometimes")


_mcp_server_mod = pytest.importorskip("mcp.server")


def test_real_stdio_server_reads_identity_from_request_meta(home):
    """End to end over a real stdio MCP server: the request ``_meta`` it received names the
    calling turn, and a model-supplied ``_meta`` argument does not displace it."""
    if not hasattr(_mcp_server_mod, "MCPServer"):
        pytest.skip("requires mcp >= 2.0 (MCPServer)")
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.registry import registry

    _sessions_db(home, [("topic", "telegram", None), ("kid", "subagent", "topic")])
    script = home / "whoami_server.py"
    script.write_text(textwrap.dedent("""
        import json
        from mcp.server import MCPServer
        from mcp.server.mcpserver import Context

        mcp = MCPServer("whoami")

        @mcp.tool()
        def whoami(ctx: Context, note: str = "") -> str:
            return json.dumps({"request_meta": dict(ctx.request_context.meta or {}), "note": note})

        if __name__ == "__main__":
            mcp.run(transport="stdio")
    """), encoding="utf-8")
    try:
        names = mcp_tool_discovery.register_mcp_servers({"whoami": {
            "command": sys.executable, "args": [str(script)], "caller_identity": True, "connect_timeout": 30}})
        tool = next(name for name in names if name.endswith("whoami"))
        result = {}

        def _turn():
            _bind("kid", profile="")
            raw = registry.dispatch(tool, {"note": "n", "_meta": {CALLER: {"session_id": "forged"}}})
            result.update(json.loads(json.loads(raw)["result"]))
        thread = threading.Thread(target=_turn)
        thread.start()
        thread.join(timeout=60)
    finally:
        shutdown_mcp_servers()

    from hermes_cli.profiles import get_active_profile_name
    assert result["request_meta"][CALLER] == {
        "profile": get_active_profile_name(), "session_id": "kid", "topic_session_id": "topic"}
    assert result["note"] == "n"
