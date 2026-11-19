"""Runtime notices preserve complete receipts through real turn/request/persistence boundaries."""
from __future__ import annotations

import copy
import json
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from agent.plugin_live_notes import queue_plugin_notice
from agent.replay_cleanup import is_interrupted_tool_result, strip_interrupted_tool_tails
from tests.agent.test_plugin_live_notes_delivery import _agent, _response, _run, _script
from tests.agent.test_tool_call_incremental_persistence import _attach_real_session_db

NOTE = "Reply now"
RAW = json.dumps({"output": "partial\n[Command interrupted]", "exit_code": 130})


@pytest.fixture(autouse=True)
def _no_background_titles(monkeypatch):
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)


def _call(call_id="probe"):
    return {"id": call_id, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}


def test_json_receipt_and_unknown_replay_survive_queued_notice():
    agent = _agent("terminal")
    requests = _script(agent, _response(tool_call_id="probe", tool_name="terminal"), _response("done"))
    _run(agent, "run", tool_result=lambda: (queue_plugin_notice(agent, "pacing", NOTE), RAW)[-1])
    tool = next(m for m in requests[1] if m["role"] == "tool")
    assert tool["content"] == RAW
    assert json.loads(tool["content"])["exit_code"] == 130
    assert is_interrupted_tool_result(tool["content"])
    cleaned = strip_interrupted_tool_tails([
        {"role": "assistant", "content": "", "tool_calls": [_call()]}, tool, requests[1][-1],
    ])
    recovered = next(m for m in cleaned if m["role"] == "tool")
    assert recovered["effect_disposition"] == "unknown"
    assert "UNKNOWN" in recovered["content"]
    assert requests[1][-1]["role"] == "user" and NOTE in requests[1][-1]["content"]


@pytest.mark.parametrize("multimodal", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_real_sdk_continuation_persists_notice_once_and_keeps_frozen_tool_bytes(tmp_path, multimodal, streaming):
    agent = _agent("terminal")
    agent._disable_streaming = not streaming
    db = _attach_real_session_db(agent, tmp_path / "state.db", "notice-sdk")
    requests, durable_at_request = [], []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        durable_at_request.append(db.get_messages_as_conversation("notice-sdk"))
        if len(requests) == 1:
            message, reason = {"role": "assistant", "content": None, "tool_calls": [_call()]}, "tool_calls"
        else:
            message, reason = {"role": "assistant", "content": "done"}, "stop"
        if payload.get("stream"):
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [{**call, "index": i} for i, call in enumerate(delta["tool_calls"])]
            chunk = {"id": "synthetic-response", "object": "chat.completion.chunk", "created": 0,
                "model": "test/model", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={
            "id": "synthetic-provider-response", "object": "chat.completion", "created": 0,
            "model": "test/model", "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        })

    clients = []

    def client_factory(kwargs, **_):
        client = OpenAI(**{**kwargs, "http_client": httpx.Client(transport=httpx.MockTransport(respond))})
        clients.append(client)
        return client

    agent.client = client_factory(agent._client_kwargs)
    raw = json.dumps({"output": "complete\n\u03bb", "exit_code": 0})
    content = [{"type": "text", "text": raw},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD", "detail": "low"}}] if multimodal else raw
    result = {"_multimodal": True, "content": content, "text_summary": raw} if multimodal else raw
    try:
        with patch.object(agent, "_model_supports_vision", return_value=True), \
                patch.object(agent, "_provider_supports_vision_tool_messages", return_value=True), \
                patch.object(agent, "_create_openai_client", side_effect=client_factory):
            first = _run(agent, "run", tool_result=lambda: (queue_plugin_notice(agent, "pacing", NOTE), result)[-1])
            _run(agent, "next", history=first["messages"])
        assert len(requests) == 3
        sent = requests[1]["messages"]
        tool = next(m for m in sent if m["role"] == "tool")
        assert tool["content"] == content and tool["tool_call_id"] == "probe"
        stored_tool = next(m for m in durable_at_request[1] if m["role"] == "tool")
        assert stored_tool["content"] == content and stored_tool["tool_call_id"] == "probe"
        notice = sent[-1]
        assert notice["role"] == "user" and NOTE in notice["content"]
        stored_notice = next(m for m in db.get_messages_as_conversation("notice-sdk") if NOTE in str(m.get("content")))
        assert stored_notice["content"] == notice["content"]
        assert stored_notice["display_kind"] == "internal_notification"
        assert all("display_kind" not in m and "display_metadata" not in m for m in sent)
        # Frozen request prefix and durable tool receipt are unchanged on the next user turn.
        assert requests[2]["messages"][:len(sent)] == sent
        assert sum(NOTE in str(m.get("content")) for m in requests[2]["messages"]) == 1
        assert next(m for m in durable_at_request[2] if m["role"] == "tool")["content"] == content
    finally:
        for client in clients:
            client.close()
        db.close()


def test_notice_and_concurrent_steer_share_one_new_row_without_human_misattribution():
    from agent.synthetic_prompt import human_prompt_text

    agent = _agent("terminal")
    requests = _script(agent, _response(tool_call_id="probe", tool_name="terminal"), _response("done"))

    def tool():
        queue_plugin_notice(agent, "pacing", NOTE)
        agent.steer("Use the other branch")
        return "complete result"

    result = _run(agent, "run", tool_result=tool)
    sent = requests[1]
    assert [m["role"] for m in sent][-3:] == ["assistant", "tool", "user"]
    row = sent[-1]
    assert row["content"].count(NOTE) == 1
    assert "Use the other branch" in row["content"]
    persisted = result["messages"][-2]
    assert persisted["display_kind"] == "steer"
    assert human_prompt_text(persisted["content"], display_kind=persisted["display_kind"]) == "Use the other branch"
    assert next(m for m in sent if m["role"] == "tool")["content"] == "complete result"


def test_step_callback_notice_reaches_this_continuation_not_next_turn():
    agent = _agent("terminal")
    requests = _script(agent, _response(tool_call_id="probe", tool_name="terminal"), _response("done"))
    steps = []

    def step(*_):
        steps.append(True)
        if len(steps) == 2:
            queue_plugin_notice(agent, "late", NOTE)

    agent.step_callback = step
    _run(agent, "run", tool_result=lambda: "complete result")
    assert len(requests) == 2
    assert NOTE in requests[1][-1]["content"]
    assert next(m for m in requests[1] if m["role"] == "tool")["content"] == "complete result"


def test_parallel_batch_notice_follows_all_results_in_call_order():
    agent = _agent("read_file")
    response = _response(tool_call_id="a", tool_name="read_file")
    response.choices[0].message.tool_calls[0].function.arguments = '{"path":"a"}'
    response.choices[0].message.tool_calls.append(SimpleNamespace(
        id="b", type="function", function=SimpleNamespace(name="read_file", arguments='{"path":"b"}')))
    requests = _script(agent, response, _response("done"))
    _run(agent, "run", tool_result=lambda: (queue_plugin_notice(agent, "pacing", NOTE), "receipt")[-1])
    sent = requests[1]
    tools = [m for m in sent if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["a", "b"]
    assert all(m["content"] == "receipt" for m in tools)
    assert [m["role"] for m in sent][-3:] == ["tool", "tool", "user"]
    assert sent[-1]["content"].count(NOTE) == 1


@pytest.mark.parametrize("content", [RAW, [{"type": "text", "text": RAW},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD", "detail": "low"}}]])
def test_native_requests_and_responses_preserve_tool_parts(content):
    from agent.codex_responses_adapter import _chat_messages_to_responses_input, _preflight_codex_input_items
    from agent.plugin_live_notes import append_continuation_notice
    from agent.agent_runtime_helpers import repair_message_sequence

    agent = SimpleNamespace()
    messages = [{"role": "user", "content": "run"},
                {"role": "assistant", "content": "", "tool_calls": [_call()]},
                {"role": "tool", "content": copy.deepcopy(content), "tool_call_id": "probe"}]
    before = copy.deepcopy(messages)
    queue_plugin_notice(agent, "plugin", NOTE)
    assert append_continuation_notice(agent, messages)
    assert messages[:-1] == before
    repair_message_sequence(agent, messages)
    assert messages[:-1] == before
    items = _preflight_codex_input_items(_chat_messages_to_responses_input(messages))
    output = next(m for m in items if m.get("type") == "function_call_output")
    assert output["call_id"] == "probe"
    if isinstance(content, str):
        assert output["output"] == content
    else:
        assert output["output"] == [{"type": "input_text", "text": RAW},
            {"type": "input_image", "image_url": content[1]["image_url"]["url"], "detail": "low"}]
    assert NOTE in str(items[-1])
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "response", "created_at": 0,
            "model": "test/model", "status": "completed", "output": []})

    with OpenAI(api_key="test", base_url="https://provider.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        client.responses.create(model="test/model", input=items)
    assert payloads[0]["input"] == items
    assert messages[:-1] == before
    assert not append_continuation_notice(agent, messages)


def test_notice_does_not_make_unread_tool_round_eligible_for_pressure_pruning():
    from agent.context_compressor import ContextCompressor
    from agent.plugin_live_notes import append_continuation_notice

    content = "complete receipt " * 1_000
    messages = [{"role": "user", "content": "run"},
                {"role": "assistant", "content": "", "tool_calls": [_call()]},
                {"role": "tool", "content": content, "tool_call_id": "probe"}]
    compressor = ContextCompressor(model="test", quiet_mode=True, config_context_length=200_000)
    before = list(compressor._spared_pending_tool_round(messages))
    assert before  # the control qualifies for the existing bounded protection
    queue_plugin_notice(agent := SimpleNamespace(), "plugin", NOTE)
    append_continuation_notice(agent, messages)
    assert list(compressor._spared_pending_tool_round(messages)) == before
    assert messages[2]["content"] == content


@pytest.mark.parametrize("prior_notice", [False, True])
@pytest.mark.parametrize("redirect_count", [1, 2])
def test_redirect_composes_pending_guidance_before_persistence(tmp_path, prior_notice, redirect_count):
    from agent.plugin_live_notes import RUNTIME_NOTICE_TEMPLATE
    from agent.synthetic_prompt import human_prompt_text

    agent = _agent("terminal")
    agent._disable_streaming = True
    db = _attach_real_session_db(agent, tmp_path / "redirect.db", "notice-redirect")
    requests, durable, tickets, guidance = [], [], [], []
    receipt = json.dumps({"output": "full receipt", "exit_code": 0})

    def model_call(kwargs):
        requests.append(copy.deepcopy(kwargs["messages"]))
        durable.append(db.get_messages_as_conversation("notice-redirect"))
        if len(requests) == 1:
            return _response(tool_call_id="probe", tool_name="terminal")
        if len(requests) <= redirect_count + 1:
            ordinal = len(requests) - 1
            steer, redirect, ticket = f"Guidance {ordinal}", f"Correction {ordinal}", f"transfer-{ordinal}"
            assert agent.steer(steer, ticket=ticket)
            tickets.append(ticket)
            guidance.append(steer)
            queue_plugin_notice(agent, "during-request", f"Host control {ordinal}")
            assert agent.redirect(redirect)
            raise InterruptedError("redirect cancelled the request")
        return _response("done")

    def tool():
        if prior_notice:
            queue_plugin_notice(agent, "earlier", "Earlier host control")
            assert agent.steer("Earlier guidance")
        return receipt

    agent._interruptible_api_call = model_call
    # This test observes redirect persistence itself. The ordinary notice helper
    # stubs _persist_session, so drive the real turn and store directly here.
    with (
        patch("agent.steer_delivery.report_steer_delivered") as delivered,
        patch("model_tools.handle_function_call", side_effect=lambda *a, **k: tool()),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        try:
            first = agent.run_conversation("run")
            rebuilt = requests[-1]
            for ordinal in range(1, redirect_count + 1):
                human = f"Guidance {ordinal}\n\nCorrection {ordinal}"
                host = RUNTIME_NOTICE_TEMPLATE.format(notes=f"Host control {ordinal}")
                sent = next(row for row in rebuilt if host in str(row.get("content")))
                assert sent["content"].endswith(human)
                stored = next(row for row in durable[-1] if host in str(row.get("content")))
                assert stored["api_content"] == sent["content"]
                assert stored["content"] == f"{host}\n\n{human}"
                assert human_prompt_text(stored["content"], display_kind=stored.get("display_kind")) == human
            for previous, following in pairwise(requests[1:]):
                assert following[:len(previous)] == previous
            assert next(row for row in rebuilt if row["role"] == "tool")["content"] == receipt
            assert json.loads(next(row for row in rebuilt if row["role"] == "tool")["content"])["exit_code"] == 0
            assert first["completed"] and first.get("pending_steer") is None
            expected = (["Earlier guidance"] if prior_notice else []) + guidance
            assert [call.args[1] for call in delivered.call_args_list] == expected
            assert all(agent.withdraw_steer(ticket) is False for ticket in tickets)
            agent.run_conversation("next", conversation_history=first["messages"])
            assert requests[-1][:len(rebuilt)] == rebuilt
        finally:
            db.close()


@pytest.mark.parametrize("scaffold", ["snapshot", "restatement"])
@pytest.mark.parametrize("with_steer", [False, True])
def test_post_tool_scaffolding_delivers_controls_on_immediate_request_without_rewriting_prefix(tmp_path, with_steer, scaffold):
    from agent.conversation_compression import _fold_todo_snapshot
    from agent.turn_preflight import PostToolCompressionVerdict
    from tools.todo_tool import TODO_INJECTION_HEADER

    agent = _agent("terminal")
    db = _attach_real_session_db(agent, tmp_path / "state.db", "notice-snapshot")
    requests = _script(agent, _response(tool_call_id="probe", tool_name="terminal"),
                       _response("done"), _response("next done"))
    snapshot = f"{TODO_INJECTION_HEADER}\n1. Continue the task"
    # A completed receipt is cache-stable across the next turn. Interrupted
    # receipts deliberately become UNKNOWN during replay in the test above.
    receipt = json.dumps({"output": "complete", "exit_code": 0})
    frozen, durable = [], []

    def post_tool_snapshot(_agent, **state):
        # Publish each producer's post-tool shape without an external summarizer.
        # The complete compressor path is exercised by the regression below.
        messages = state["messages"]
        if scaffold == "snapshot":
            _fold_todo_snapshot(agent, messages)
            assert messages[-1].get("_todo_snapshot_synthetic") is True
        else:
            from agent.context_compressor import _INFLIGHT_TASK_REPLAY_HEADER
            from agent.message_metadata import mark_inflight_restatement

            replay = {"role": "user", "content": f"{_INFLIGHT_TASK_REPLAY_HEADER}\nrun"}
            mark_inflight_restatement(replay)
            messages.append(replay)
        agent._flush_messages_to_session_db(messages, state["conversation_history"])
        frozen.extend(copy.deepcopy(messages))
        durable.extend(db.get_messages_as_conversation("notice-snapshot"))
        return PostToolCompressionVerdict(
            end_turn=False, messages=messages, active_system_prompt=state["active_system_prompt"],
            conversation_history=state["conversation_history"], compression_attempts=1,
            final_response=state["final_response"], turn_exit_reason=state["turn_exit_reason"],
            current_turn_user_idx=state["current_turn_user_idx"],
        )

    def tool(*_args, **_kwargs):
        queue_plugin_notice(agent, "pacing", NOTE)
        if with_steer:
            assert agent.steer("Use the other branch", ticket="snapshot-transfer")
        return receipt

    with (
        patch.object(vars(agent)["_todo_store"], "format_for_injection", return_value=snapshot),
        patch("agent.turn_tool_round.compress_after_tool_results", side_effect=post_tool_snapshot),
        patch("model_tools.handle_function_call", side_effect=tool),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.steer_delivery.report_steer_delivered") as delivered,
    ):
        try:
            first = agent.run_conversation("run")
            sent = requests[1]
            assert sum(NOTE in str(row.get("content")) for row in sent) == 1
            assert NOTE in sent[-1]["content"]
            assert next(row for row in sent if row["role"] == "tool")["content"] == receipt
            assert first["messages"][:len(frozen)] == frozen
            stored = db.get_messages_as_conversation("notice-snapshot")
            assert stored[:len(durable)] == durable
            assert [row["role"] for row in stored[len(durable):]][:2] == ["assistant", "user"]
            assert stored[len(durable)]["display_kind"] == "hidden"
            assert delivered.call_count == int(with_steer)
            assert agent.withdraw_steer("snapshot-transfer") is False
            assert first.get("pending_steer") is None
            if with_steer:
                assert "Use the other branch" in sent[-1]["content"]
            agent.run_conversation("next", conversation_history=first["messages"])
            assert requests[-1][:len(sent)] == sent
            assert sum(NOTE in str(row.get("content")) for row in requests[-1]) == 1
        finally:
            db.close()


@pytest.mark.parametrize("with_steer", [False, True])
def test_real_compressor_restatement_keeps_controls_on_its_next_continuation(with_steer):
    from agent.message_metadata import INFLIGHT_TASK_RESTATEMENT
    from agent.plugin_live_notes import append_continuation_notice, pop_plugin_notices
    from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result
    from tests.agent.test_cron_inflight_prompt_reappend import _compress, _cron_transcript

    source = _cron_transcript()
    receipt = json.dumps({"output": "complete receipt", "exit_code": 0})
    source[-1]["content"] = receipt
    messages = _compress(source)
    assert messages[-1].get("display_metadata", {}).get(INFLIGHT_TASK_RESTATEMENT) is True
    assert next(row for row in reversed(messages) if row["role"] == "tool")["content"] == receipt
    frozen = copy.deepcopy(messages)
    agent = _agent("terminal")
    queue_plugin_notice(agent, "pacing", NOTE)
    if with_steer:
        assert agent.steer("Use the other branch", ticket="restatement-transfer")
    with patch("agent.steer_delivery.report_steer_delivered") as delivered:
        _inject_steer_after_newest_tool_result(agent, messages, agent._drain_pending_steer())
    assert messages[:len(frozen)] == frozen
    assert messages[-1]["role"] == "user" and NOTE in messages[-1]["content"]
    assert messages[-2]["role"] == "assistant" and messages[-2]["display_kind"] == "hidden"
    assert delivered.call_count == int(with_steer)
    assert agent.withdraw_steer("restatement-transfer") is False
    assert agent._drain_pending_steer() is None and pop_plugin_notices(agent) == ""
    if with_steer:
        assert "Use the other branch" in messages[-1]["content"]
    assert not append_continuation_notice(agent, messages)


@pytest.mark.parametrize("tail_kind", ["human", "pasted-restatement", "merged-handoff"])
def test_deferred_continuation_preserves_steer_transfer_ticket(tail_kind):
    from agent.context_compressor import (
        _INFLIGHT_TASK_REPLAY_HEADER, _SUMMARY_END_MARKER, SUMMARY_PREFIX, user_originated_turn_view,
    )
    from agent.plugin_live_notes import pop_plugin_notices
    from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result

    tail: dict = {"role": "user", "content": "run"}
    if tail_kind == "pasted-restatement":
        tail["content"] = f"{_INFLIGHT_TASK_REPLAY_HEADER}\nrun"
    elif tail_kind == "merged-handoff":
        tail.update(content=f"{SUMMARY_PREFIX}\nhistory\n{_SUMMARY_END_MARKER}\nrun",
                    _compressed_summary=True, display_kind="hidden")
    assert user_originated_turn_view(tail) is not None
    agent = _agent("terminal")
    queue_plugin_notice(agent, "pacing", NOTE)
    assert agent.steer("not yet deliverable", ticket="transfer")
    steer = agent._drain_pending_steer()
    messages = [copy.deepcopy(tail)]
    _inject_steer_after_newest_tool_result(agent, messages, steer)
    assert messages == [tail]
    assert agent.withdraw_steer("transfer") is True
    assert agent._drain_pending_steer() is None
    assert pop_plugin_notices(agent) == NOTE
