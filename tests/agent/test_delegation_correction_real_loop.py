"""Real run_conversation, SDK transport and executor; no fake correction turn."""
import json
from copy import deepcopy

import httpx
import pytest
from openai import OpenAI


@pytest.mark.parametrize("mode,repair_requests", [("handle", 2), ("omit", 2), ("invalid", 2), ("error", 1), ("prose", 1), ("interrupt", 1), ("transport_error", 1)])
@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_responses"])
def test_internal_repair_is_silent_and_physically_bounded(tmp_path, monkeypatch, mode, repair_requests, api_mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA
    requests, visible, speech, clients = [], [], [], []
    missing = [{"parent_task_id": "task", "thread_ref": "A", "attempt": 0}]
    recorded = []

    def progress(event, *args, **kw):
        if event == "subagent.result_turn":
            return {"missing": deepcopy(missing)}
        if event == "subagent.handling":
            if mode == "interrupt":
                # Interrupt inside an outstanding handle, through the native
                # turn interrupt path, not a fabricated correction function.
                agent._interrupt_requested = True
                agent._vprint("SECRET_INTERRUPTION", force=True)
                agent._safe_print("SECRET_DIRECT")
                raise InterruptedError("fixture interrupted handle")
            recorded.append(kw)
            missing.clear()
            return {"recorded": True}
        visible.append((event, args, kw))

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        ordinal = len(requests)
        if ordinal > 1 and api_mode == "chat_completions":
            assert not body.get("stream"), "internal correction must not stream"
        if ordinal == 1:
            message = {"role": "assistant", "content": "Original useful answer."}
        elif mode == "transport_error":
            raise httpx.RemoteProtocolError("fixture disconnected stream")
        elif mode == "error":
            return httpx.Response(500, json={"error": {"message": "fixture failure"}})
        elif mode == "prose" or (mode == "handle" and ordinal == 3):
            message = {"role": "assistant", "content": "SECRET_FINAL"}
        else:
            args = {"action": "handle", "parent_task_id": "task", "handled_refs": ["A" if mode in {"handle", "interrupt"} else "B"], "handling": "incorporated"}
            message = {"role": "assistant", "content": "SECRET_INTERIM", "tool_calls": [
                {"id": f"call-{ordinal}", "type": "function", "function": {"name": "SECRET_invalid_tool" if mode == "invalid" else "delegate_task", "arguments": json.dumps(args)}}]}
        reason = "tool_calls" if message.get("tool_calls") else "stop"
        if api_mode == "codex_responses":
            output = [{"type": "message", "id": "msg", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": message["content"], "annotations": []}]}]
            output.extend({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                           "status": "completed", **tc["function"]} for tc in message.get("tool_calls", []))
            response = {"id": "response", "object": "response", "created_at": 1, "status": "completed",
                        "model": "test-model", "output": output}
            events = [{"type": "response.output_text.delta", "delta": message["content"], "item_id": "msg", "output_index": 0, "content_index": 0}]
            events.extend({"type": "response.output_item.done", "output_index": i, "item": item} for i, item in enumerate(output))
            events.append({"type": "response.completed", "response": response})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="".join("data: " + json.dumps(e) + "\n\n" for e in events))
        if body.get("stream"):
            chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "test-model", "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                      {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "test-model", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1, "model": "test-model", "choices": [{"index": 0, "message": message, "finish_reason": reason}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    agent = AIAgent(api_key="test-key", base_url="http://fixture.invalid/v1", provider="openai-compat", api_mode=api_mode, model="test-model", quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=8, session_id="repair-fixture")
    def create(kwargs, **ignored):
        client = OpenAI(**{**kwargs, "api_key": "test-key", "http_client": httpx.Client(transport=httpx.MockTransport(respond))})
        clients.append(client)
        return client
    monkeypatch.setattr(agent, "_create_openai_client", create)
    agent._close_cached_request_openai_client(reason="fixture")
    agent._client_kwargs = {"api_key": "test-key", "base_url": "http://fixture.invalid/v1"}
    agent.tools = [{"type": "function", "function": DELEGATE_TASK_SCHEMA}]
    agent.valid_tool_names = {"delegate_task"}
    agent.tool_progress_callback = progress
    agent.stream_delta_callback = lambda x: visible.append(("delta", x))
    agent.interim_assistant_callback = lambda x, **kw: visible.append(("interim", x))
    agent.reasoning_callback = lambda x, **kw: visible.append(("reasoning", x))
    agent._pending_delegation_presentations = [{"parent_task_id": "task", "thread_refs": ["A"], "attempts": {"A": 0}}]
    agent._print_fn = lambda *args, **kw: visible.append(("print", args))
    agent.suppress_status_output = False
    snapshots = {name: getattr(agent, name) for name in ("_print_fn", "suppress_status_output", "tool_progress_callback", "stream_delta_callback", "interim_assistant_callback", "reasoning_callback", "max_iterations", "quiet_mode")}
    # Hosts install their TTS sink through this public stream_callback channel.
    try:
        result = agent.run_conversation("Answer briefly.", stream_callback=speech.append)
        assert result["final_response"].startswith("Original useful answer."), result
        assert len(requests) == 1 + repair_requests, requests
        assert result["api_calls"] == len(requests)
        assert "Original useful answer." in repr(visible)
        assert "Original useful answer." in repr(speech)
        assert "SECRET" not in repr(visible) + repr(speech)
        if api_mode == "chat_completions":
            assert requests[0]["messages"][0] == requests[1]["messages"][0]
        else:
            assert requests[0]["instructions"] == requests[1]["instructions"]
        assert requests[0]["tools"] == requests[1]["tools"]
        assert bool(recorded) == (mode == "handle")
        assert ("still need an explicit disposition" in result["final_response"]) == (mode != "handle")
        for name, value in snapshots.items():
            assert getattr(agent, name) == value, name
        assert any(m.get("display_kind") == "hidden" for m in result["messages"])
        assert all(m.get("display_kind") == "hidden" for m in result["messages"] if "SECRET" in repr(m))
    finally:
        for client in clients:
            client.close()
        agent.close()


def test_virtual_transport_rejected_before_repair_and_at_physical_dispatch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent.copilot_acp_client import CopilotACPClient
    from agent.delegation_correction import run_correction
    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    client = CopilotACPClient(max_retries=0, acp_cwd=str(tmp_path))
    launched = []
    monkeypatch.setattr(client, "_run_prompt", lambda *a, **kw: launched.append(True))
    agent = SimpleNamespace(api_mode="chat_completions", provider="copilot-acp", client=client,
                            _delegation_disposition_correction={"task": ["A"]})
    history = [{"role": "assistant", "content": "Original"}]
    with pytest.raises(ValueError, match="bounded disposition"):
        run_correction(agent, "repair", None, history, "task")
    assert history == [{"role": "assistant", "content": "Original"}]
    assert not hasattr(agent, "_session_messages")  # no staged/persisted correction
    with pytest.raises(ValueError, match="bounded disposition"):
        _dispatch_nonstreaming_api_request(agent, {}, make_client=lambda *a, **kw: client)
    assert not launched
    client.close()
