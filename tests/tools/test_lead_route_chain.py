"""Inherited authority is frozen before a real named-child launch."""
import json
from types import SimpleNamespace
import pytest
import yaml
from tests.run_agent.test_custom_subagent_runtime import make_child
from tools.delegate_tool import _preflight_task_runtime, _build_child_agent

@pytest.fixture
def fixture_endpoint():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "gpt-5.6-terra", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Verified inherited fallback."}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("provider,model,mode,effort", [("custom", "gpt-5.6-terra", "chat_completions", None), ("anthropic", "claude-opus-4-8", "anthropic_messages", "high")])
@pytest.mark.parametrize("named", [False, True])
def test_inherited_authority(make_child, tmp_path, monkeypatch, named, provider, model, mode, effort, fixture_endpoint):
    parent = make_child("high", "gpt-6-astra")
    del parent._delegation_runtime_pin
    parent._fallback_index = 1
    parent._fallback_chain = [{"provider": "consumed", "model": "never-resolve"}, {"provider": provider, "model": model,
        "base_url": fixture_endpoint, "api_key": "fixture-only", "api_mode": mode}]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"agent": {"reasoning_effort": "high"}}))
    cfg = {"provider": "not-inherited", "subagents": {"lead": {
        "description": "Fixture", "instructions": "Fixture", "inherit_parent": True}}}
    launches, error = _preflight_task_runtime([{"subagent_type": "lead"}], cfg, None, parent, {})
    assert error is None
    assert len(launches[0].fallback_routes) == 1
    if named:
        routes = launches[0].fallback_routes
        parent._delegation_runtime_pin = SimpleNamespace(fallback_routes=routes)
        parent._fallback_chain = [r.native_entry() for r in routes]
        parent._fallback_index = 0
        monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider",
                            lambda **kw: pytest.fail("re-resolved frozen authority"))
        launches, error = _preflight_task_runtime([{"subagent_type": "lead"}], cfg, None, parent, {})
        assert error is None
    launch = launches[0]
    parent._fallback_chain[-1]["api_key"] = "mutated"
    (tmp_path / "config.yaml").write_text("agent: {reasoning_effort: low}\n")
    child = _build_child_agent(task_index=0, goal="Fixture", context=None, toolsets=None,
        model=parent.model, max_iterations=1, task_count=1, parent_agent=parent,
        subagent_definition=launch.definition, resolved_reasoning=launch.reasoning,
        resolved_fallback_routes=launch.fallback_routes)
    try:
        assert child.memory_access_mode == "read_only"
        assert child._try_activate_fallback()
        kwargs = child._build_api_kwargs([{"role": "user", "content": "Fixture"}], tools_for_api=[])
        child._delegation_runtime_pin.validate_request(child, kwargs, client=child._anthropic_client if mode == "anthropic_messages" else child.client)
        if mode == "chat_completions":
            response = child.client.chat.completions.create(**kwargs)
            assert response.choices[0].message.content == "Verified inherited fallback."
        assert child.api_key == "fixture-only"
        assert (child.reasoning_config or {}).get("effort") == effort
        assert "fixture-only" not in json.dumps(child._delegation_runtime_pin.metadata())
    finally:
        child.close()

@pytest.mark.parametrize("fallbacks", [[], [{"provider": "custom", "model": "replacement"}]])
def test_explicit_authority_wins(make_child, monkeypatch, fallbacks):
    parent = make_child()
    parent._fallback_chain = [{"provider": "forbidden", "model": "parent-only"}]
    def resolve(**kw):
        assert kw["requested"] == "custom"
        return {"provider": "custom", "model": "replacement", "base_url": "http://127.0.0.1:9/v1",
                "api_mode": "chat_completions", "api_key": "fixture"}
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)
    cfg = {"subagents": {"lead": {"description": "Fixture", "instructions": "Fixture",
                                  "inherit_parent": True, "fallbacks": fallbacks}}}
    launches, error = _preflight_task_runtime([{"subagent_type": "lead"}], cfg, None, parent, {})
    assert error is None
    assert len(launches[0].fallback_routes) == len(fallbacks)