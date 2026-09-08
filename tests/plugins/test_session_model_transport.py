"""Actual plugin/agent/frontends with a loopback OpenAI-compatible HTTP fixture."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest
import yaml


@pytest.mark.parametrize("frontend", ["cli", "gateway"])
def test_model_round_trip(tmp_path, monkeypatch, frontend):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass
        def do_GET(self):
            raw = json.dumps({"data": [
                {"id": "fixture-large", "context_length": 100000},
                {"id": "fixture-small", "context_length": 16000}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            requests.append(data)
            msg = {"role": "assistant", "content": "Ready."}
            finish = "stop"
            if len(requests) == 1:
                msg = {"role": "assistant", "tool_calls": [{"index": 0, "id": "c1", "type": "function",
                    "function": {"name": "tool_call", "arguments": json.dumps({"name": "session_model",
                    "arguments": {"model": "small"}})}}]}
                finish = "tool_calls"
            body = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                "model": data["model"], "choices": [{"index": 0, "delta": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            raw = ("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    config = {"plugins": {"enabled": ["session-model"]},
        "model": {"default": "fixture-large", "provider": "custom", "base_url": url},
        "custom_providers": [{"name": "fixture", "base_url": url, "api_key": "fixture-key",
            "models": [{"id": "fixture-large", "context_length": 100000}, {"id": "fixture-small", "context_length": 16000}]}],
        "model_aliases": {"small": {"model": "fixture-small", "provider": "custom:fixture"}}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    original = path.read_bytes()
    from run_agent import AIAgent
    from hermes_cli.session_model import session_model_scope
    agent = AIAgent(model="fixture-large", provider="custom", api_mode="chat_completions",
        api_key="fixture-key", base_url=url, enabled_toolsets=["session_model"], session_id="fixture",
        skip_memory=True, skip_context_files=True, quiet_mode=True, save_trajectories=False, max_iterations=4)
    agent.reasoning_config = None
    agent._primary_runtime["reasoning_config"] = None
    if frontend == "cli":
        from cli import HermesCLI
        from hermes_cli.session_model_cli import apply_session_model
        owner = object.__new__(HermesCLI)
        owner.agent, owner.model = agent, agent.model
        owner.provider, owner.api_mode = agent.provider, agent.api_mode
        owner.api_key, owner.base_url = agent.api_key, agent.base_url
        owner.session_id, owner._session_db = agent.session_id, None
        apply = lambda selection: apply_session_model(owner, agent, selection)
    else:
        from gateway.session_model import apply_session_model
        from tests.gateway.test_model_switch_persistence import _make_runner, _make_source
        from gateway.session import build_session_key
        owner, source = _make_runner(), _make_source()
        key = build_session_key(source)
        owner._running_agents[key] = agent
        owner._session_reasoning_overrides = {}
        apply = lambda selection: asyncio.run(apply_session_model(owner, agent, source, key, selection))
    try:
        with session_model_scope(agent, apply) as control:
            result = agent.run_conversation("Switch to small.", task_id=agent.session_id)
            rows = [m for m in result["messages"] if m["role"] == "tool"]
            assert rows, result
            receipt = json.loads(rows[-1]["content"])
            assert receipt["status"] == "queued", receipt
            assert agent.model == "fixture-large"
            result = control.finish(result)
        assert result["session_model"]["status"] == "applied", result
        assert all(req["model"] == "fixture-large" for req in requests)
        second = agent.run_conversation("Continue.", conversation_history=result["messages"], task_id=agent.session_id)
        assert second["completed"], second
        assert requests[-1]["model"] == "fixture-small"
        assert path.read_bytes() == original
        if frontend == "gateway":
            assert owner._session_model_overrides[key]["model"] == "fixture-small"
        else:
            assert owner.model == "fixture-small"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
