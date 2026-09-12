"""Real agents, outbound HTTP serialization, dispatch and child runner. No paid API."""
from copy import deepcopy
import json

import httpx
from openai import OpenAI
import pytest


@pytest.mark.parametrize("mode,review,expected", [(None, False, "fork"),
    ("fresh", False, "fresh"), ("fork", True, "fresh")])
def test_real_dispatch_child_receives_reference_and_keeps_own_authority(tmp_path, monkeypatch, mode, review, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("""model:
  default: test-model
delegation:
  max_spawn_depth: 2
  max_iterations: 4
  subagents:
    owner:
      description: Fixture owner
      instructions: CHILD_SCOPE_ONLY
      inherit_parent: true
""")
    from run_agent import AIAgent
    from tools import delegate_tool as dt
    from tools.delegation_history import FORK_REFERENCE
    from tools.registry import registry
    requests, clients = [], []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        msg = {"role": "assistant", "content": "Fixture answer."}
        if body.get("stream"):
            chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                       "model": "test-model", "choices": [{"index": 0, "delta": msg, "finish_reason": None}]},
                      {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                       "model": "test-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "test-model", "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def create(self, kwargs, **ignored):
        client = OpenAI(**{**kwargs, "api_key": "test-key",
            "http_client": httpx.Client(transport=httpx.MockTransport(respond))})
        clients.append(client)
        return client

    monkeypatch.setattr(AIAgent, "_create_openai_client", create)
    parent = AIAgent(api_key="test-key", base_url="http://fixture.invalid/v1", provider="openai-compat",
        api_mode="chat_completions", model="test-model", quiet_mode=True, skip_memory=True,
        skip_context_files=True, save_trajectories=False, max_iterations=4, session_id="fork-parent",
        enabled_toolsets=["file", "skills"], ephemeral_system_prompt="PARENT_ONLY_DIRECTIVE")
    try:
        result = parent.run_conversation("CURRENT_ACCEPTED_CORRECTION")
        assert result["final_response"] == "Fixture answer.", result
        assert len(requests) == 1
        captured = deepcopy(parent._delegation_visible_window)
        # Changing the capability route must not change the history source.
        parent.model = "child-model-after-switch"
        # A durable transcript contains the current response/in-flight round;
        # neither belongs to the already-model-visible request being forked.
        parent._session_messages.append({"role": "assistant", "tool_calls": [{"id": "in-flight", "function": {"name": "delegate_task", "arguments": "{}"}}]})
        task = {"goal": "Inspect fixture only and return the result", "task_label": "Check fixture", "subagent_type": "owner"}
        if mode:
            task["context_mode"] = mode
        # Registry dispatch selects synchronous execution in a nested owner. This
        # exercises its real handler, batch builder, worker thread and child loop.
        parent._delegate_depth = 1
        parent._delegate_max_spawn_depth = 2
        if review:
            raw = dt.delegate_task(tasks=[task], parent_agent=parent, child_tool_policy="inspection_only", background=False)
        else:
            raw = registry.dispatch("delegate_task", {"tasks": [task]}, parent_agent=parent)
        assert isinstance(raw, str)
        payload = json.loads(raw)
        assert "error" not in payload, payload
        assert payload["results"][0]["status"] == "completed", payload
        assert payload["delegation_metadata"]["threads"][0]["context_mode"] == expected
        assert len(requests) == 2, payload
        child_wire = requests[-1]
        assert child_wire["model"] == "child-model-after-switch"
        texts = repr(child_wire["messages"])
        system = repr([m for m in child_wire["messages"] if m["role"] == "system"])
        assert "CHILD_SCOPE_ONLY" in system
        assert "PARENT_ONLY_DIRECTIVE" not in texts
        assert "in-flight" not in texts
        assert ("CURRENT_ACCEPTED_CORRECTION" in texts) == (expected == "fork")
        assert (FORK_REFERENCE.splitlines()[0] in texts) == (expected == "fork")
        assert parent._delegation_visible_window == captured
        assert "review_changes" not in {t["function"]["name"] for t in child_wire.get("tools", [])}
    finally:
        parent.close()
        for client in clients:
            client.close()
