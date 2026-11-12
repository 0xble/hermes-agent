"""Model delegation joins private helpers; only inference is replaced in the join test."""
import json
import sqlite3
from contextvars import Context
from types import SimpleNamespace

import pytest

from tests.agent.test_run_agent import _mock_response, _mock_tool_call
from tests.gateway.test_incognito_turn_e2e import (
    harness,
    incognito,
    private_route,
    started,
)


class _HelperProvider:
    def __init__(self):
        self.priming = True
        self.observed = []
        self.joined = []
        self.errors = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def __call__(self, *args, **kwargs):
        return self

    def close(self):
        pass

    def create(self, **kwargs):
        try:
            response = self._response(**kwargs)
        except Exception as exc:
            self.errors.append(str(exc))
            raise
        if not kwargs.get("stream"):
            return response
        from openai.types.chat import ChatCompletionChunk
        choice = response.choices[0]
        message = choice.message
        delta = {"content": message.content}
        if message.tool_calls:
            delta["tool_calls"] = [{"index": index, "id": call.id, "type": "function", "function": {
                "name": call.function.name, "arguments": call.function.arguments,
            }} for index, call in enumerate(message.tool_calls)]
        chunk = ChatCompletionChunk(id="private-helper", model="private-model", created=1,
                                    object="chat.completion.chunk", choices=[{
                                        "index": 0, "delta": delta, "finish_reason": choice.finish_reason,
                                    }])
        return (part for part in [chunk])

    def _response(self, **kwargs):
        if self.priming:
            return _mock_response(content="parent ready")
        from agent.incognito import current_session_id, is_incognito, is_registered
        from agent.incognito_private_dir import private_dir_for_session
        sid = current_session_id()
        interval = private_dir_for_session(sid, create=False)
        self.observed.append((sid, interval, is_registered(sid)))
        assert is_incognito() and interval is not None
        (interval / f"{sid}.txt").write_text("PRIVATE_HELPER_CANARY")
        messages = kwargs["messages"]
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if "CHILD_HELPER" in user:
            return _mock_response(content="CHILD_RESULT")
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if results:
            assert results[-1]["results"][0]["summary"] == "CHILD_RESULT", results
            self.joined.append("CHILD_RESULT")
            return _mock_response(content="PARENT_JOINED")
        assert any(t["function"]["name"] == "delegate_task" for t in kwargs["tools"])
        return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[
            _mock_tool_call("delegate_task", json.dumps({"goal": "CHILD_HELPER"}), call_id="call_helper"),
        ])


def _durable_state(home):
    """Include all state.db tables, notably transcripts, async units and schedule records."""
    with sqlite3.connect(home / "state.db") as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        rows = {name: connection.execute('SELECT * FROM "' + name + '"').fetchall() for name, in tables}
    files = {str(p.relative_to(home)): p.read_bytes()
             for folder in ("trajectories", "cache/delegation", "cron")
             for p in (home / folder).rglob("*") if p.is_file()}
    files.update({str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*.jsonl")})
    return rows, files


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["intercept", "registry"])
@pytest.mark.parametrize("privacy_source", ["parent", "session"])
async def test_private_model_helper_joins_without_durable_state(
        request, monkeypatch, tmp_path, route, privacy_source):
    from agent import incognito_private_dir as private_dirs
    from agent.incognito import is_incognito, register_session
    from gateway.incognito import event_scope
    from tools.delegate_tool import delegate_task
    from tools.registry import registry

    runner, _store, entry, config, event, db = request.getfixturevalue("private_route")
    config["platform_toolsets"]["telegram"] = ["delegation"]
    config["delegation"] = {"max_spawn_depth": 2, "orchestrator_enabled": True}
    config["streaming"] = {"enabled": False}
    import hermes_yaml
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    provider = _HelperProvider()
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", provider)
    started(runner, db)
    assert "ON" in await incognito(runner, event, "/incognito on")
    sid, key = entry.session_id, entry.session_key
    interval = private_dirs.private_dir_for_session(sid, create=False)
    runner._session_state(key).persistent.run_generation = 1
    incoming = event("prime private parent")
    with event_scope(runner, incoming):
        assert await runner._handle_message_with_agent(incoming, incoming.source, key, 1) == "parent ready"
    parent = runner._cached_agent_for(key)
    assert parent._delegate_depth == 0
    provider.priming = False
    before = _durable_state(tmp_path)
    bound = []
    real_bind = private_dirs.bind_private_session

    def bind(child_sid, path):
        bound.append((child_sid, path))
        return real_bind(child_sid, path)

    monkeypatch.setattr(private_dirs, "bind_private_session", bind)
    # Drop ambient ContextVars. Either stable parent state or session membership alone must suffice.
    if privacy_source == "session":
        parent.incognito = False
    assert Context().run(is_incognito) is False
    try:
        if route == "intercept":
            result = Context().run(parent.run_conversation, "Run a helper and consume its result")
            assert not provider.errors, provider.errors[0]
            assert result["final_response"] == "PARENT_JOINED", (result, provider.errors)
            assert provider.joined == ["CHILD_RESULT"]
        else:
            result = json.loads(Context().run(
                registry.dispatch, "delegate_task", {"goal": "CHILD_HELPER", "background": True},
                parent_agent=parent, session_id=sid,
            ))
            assert result.get("results", [{}])[0].get("summary") == "CHILD_RESULT", result
            assert provider.joined == []
        assert len(bound) == 1 and all(path == interval for _, path in bound)
        child_ids = {child_sid for child_sid, _ in bound}
        assert len(child_ids) == 1 and sid not in child_ids
        assert {path for _, path, _ in provider.observed} == {interval}
        assert all(not registered for observed_sid, _, registered in provider.observed if observed_sid != sid)
        assert all(private_dirs.private_dir_for_session(child_sid, create=False) is None for child_sid in child_ids)
        for kwargs in ({"background": True}, {"action": "resume", "subagent_id": "not-durable"}):
            refused = json.loads(Context().run(delegate_task, goal="must not persist", parent_agent=parent, **kwargs))
            assert "durable async delegation" in refused["error"].lower()
    finally:
        parent.incognito = True
        register_session(sid)
        register_session(key)
        assert "OFF" in await incognito(runner, event, "/incognito off")
    assert not interval.exists()
    assert _durable_state(tmp_path) == before
    assert not list(private_dirs._configured_root().glob(private_dirs.PRIVATE_DIR_PREFIX + "*"))
    assert all(b"PRIVATE_HELPER_CANARY" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize("route", ["intercept", "registry"])
@pytest.mark.parametrize("depth,privacy,expected", [
    (0, None, True), (1, None, False), (0, "parent", False),
    (0, "session", False), (0, "scope", False),
])
def test_model_routing_preserves_privacy_background_and_orchestrator_join(
        monkeypatch, route, depth, privacy, expected):
    from agent.incognito import incognito_scope, register_session
    from run_agent import AIAgent
    from tools.registry import registry
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        return "{}"

    monkeypatch.setattr("tools.delegate_tool.delegate_task", capture)
    parent = SimpleNamespace(_delegate_depth=depth, incognito=privacy == "parent", session_id="model-routing")
    args: dict = {"goal": "helper"}
    if privacy is None:
        args["background"] = not expected
    if privacy == "session":
        register_session(parent.session_id)
    try:
        with incognito_scope(privacy == "scope"):
            if route == "intercept":
                AIAgent._dispatch_delegate_task(parent, args)
            else:
                registry.dispatch("delegate_task", args, parent_agent=parent, session_id=parent.session_id)
        assert len(calls) == 1 and calls[0]["background"] is expected
    finally:
        register_session(parent.session_id, False)
