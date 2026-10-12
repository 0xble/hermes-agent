"""Gateway-owned recovery: a folded human ask gets one fresh execution, not an agent-loop nudge."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_turn import _unexpected_silence_reply
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from tests.agent.test_run_agent import _mock_response, _mock_tool_call
from tests.gateway.test_stale_finalize_suppression import FinalizeCaptureAdapter, _make_runner

SESSION_KEY = "agent:main:telegram:group:-1001:212659"
RELAY_TEXT = "[relay from=session/cron receipt=r-1]\nAI & Agents Brief. FYI, no reply needed."
TYPED_TEXT = "what did the brief say about agents?"
ANSWER = "The brief covered agent evals."


@pytest.fixture
def harness(monkeypatch, tmp_path):
    import hermes_yaml as yaml
    from run_agent import AIAgent

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "display": {"tool_progress": "off", "interim_assistant_messages": False},
        "streaming": {"enabled": True, "edit_interval": 0.01, "buffer_threshold": 1},
    }))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "test-key"})
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.valid_tool_names = {"web_search"}
    agent.tools = [{"type": "function", "function": {"name": "web_search", "description": "test search",
                    "parameters": {"type": "object", "properties": {}}}}]
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    for name in ("_persist_session", "_save_trajectory", "_cleanup_task_resources"):
        monkeypatch.setattr(agent, name, MagicMock())
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **kw: "ok")
    adapter = FinalizeCaptureAdapter()
    adapter.deleted = []

    async def delete(chat_id, message_id):
        adapter.deleted.append(message_id)
        return True

    monkeypatch.setattr(adapter, "delete_message", delete)
    runner = _make_runner(adapter)
    runner._draining = False
    state = runner._session_state(SESSION_KEY)
    state.persistent.run_generation = 1
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345",
                           thread_id="212659")
    assert runner._session_key_for_source(source) == SESSION_KEY
    event = MessageEvent(text=RELAY_TEXT, source=source, message_id="relay-1", reply_expected=False)
    state.turn.event = event
    calls, consumers = [], []

    def resolve(turn_runner, route, platform_key, ephemeral, *args):
        ctx = turn_runner._ctx
        ctx.agent_holder[0] = agent
        ctx.tools_holder[0] = agent.tools
        agent.ephemeral_system_prompt = ephemeral
        agent.session_id = ctx.session_id
        state.turn.agent, state.turn.ctx = agent, ctx
        calls.append(ctx)
        return agent, False

    monkeypatch.setattr(TurnRunner, "_resolve_turn_agent", resolve)
    import gateway.stream_consumer as stream_mod
    consumer_type = stream_mod.GatewayStreamConsumer

    def capture_consumer(*args, **kwargs):
        consumer = consumer_type(*args, **kwargs)
        if getattr(adapter, "_silence_test_transport", None) == "draft":
            consumer.cfg.transport = "draft"
        consumers.append(consumer)
        return consumer

    monkeypatch.setattr(stream_mod, "GatewayStreamConsumer", capture_consumer)
    return SimpleNamespace(agent=agent, runner=runner, adapter=adapter, event=event,
                           state=state, calls=calls, consumers=consumers)


async def _run(h, finals, *, typed=True, preamble=False, display_kind=None, first_tool=True):
    stages = iter(finals)
    first = True

    def model(*args, **kwargs):
        nonlocal first
        if first and first_tool:
            first = False
            if typed:
                event = MessageEvent(text=TYPED_TEXT, source=h.event.source, message_id="typed-1")
                assert h.runner._human_preempts_autonomous_turn(event, h.state)
                assert h.runner._steer_running_agent(h.agent, TYPED_TEXT)
                assert h.runner._fold_into_running_turn(h.agent, SESSION_KEY, event) is h.state.turn
                # A redirect/steer may re-anchor the live turn; the retry must carry that anchor.
                h.state.turn.ctx.event_message_id = "typed-1"
                h.state.turn.ctx.inbound_message_id = "typed-1"
            if preamble:
                response = _mock_response(content="Checking the brief.", finish_reason="tool_calls",
                    tool_calls=[_mock_tool_call(name="web_search", arguments="{}", call_id="original-tool")])
            else:
                response = _mock_response(content="", finish_reason="tool_calls",
                    tool_calls=[_mock_tool_call(name="web_search", arguments="{}", call_id="original-tool")])
        else:
            stage = next(stages)
            response = _mock_response(content=stage, finish_reason="stop") if isinstance(stage, str) else stage
        if callable(getattr(h, "model_hook", None)):
            h.model_hook(response)
        content = response.choices[0].message.content
        if content and h.agent.stream_delta_callback:
            h.agent._fire_stream_delta(content)
            time.sleep(0.08)  # allow a real preview tick before authoritative finish()
        return response

    h.agent.client.chat.completions.create.side_effect = model
    # Replace the provider boundary, not its client below the streaming parser: the fake returns
    # a completed response after emitting deltas exactly once, as the real boundary does.
    h.agent._interruptible_api_call = h.agent.client.chat.completions.create
    return await h.runner._run_agent(message=h.event.text, context_prompt="", history=[],
        source=h.event.source, session_id="silence-test", session_key=SESSION_KEY,
        run_generation=1, reply_expected=h.event.reply_expected,
        persist_user_display_kind=display_kind, event_message_id="relay-1", inbound_message_id="relay-1")


async def _shape(h, result):
    return await h.runner._hmwa_shape_agent_response(result, h.event.source, history=[],
        session_entry=SimpleNamespace(session_id=result.get("session_id")), session_key=None,
        _quick_key=SESSION_KEY, run_generation=1, _run_start_session_id=result.get("session_id"),
        _platform_name="telegram", _msg_start_time=0.0, reply_expected=h.event.reply_expected,
        persist_user_display_kind=h.calls[0].persist_user_display_kind)


def _payloads(h):
    return ([call["content"] for call in h.adapter.sent + h.adapter.edits]
            + [frame["text"] for frame in getattr(h.adapter, "frames", [])])


def _cumulative_adapter(h, transport, monkeypatch):
    from gateway.platforms.base import SendResult

    adapter = h.adapter
    adapter.frames = []
    if transport == "native":
        monkeypatch.setattr(type(adapter), "SUPPORTS_NATIVE_STREAMING", True, raising=False)
        adapter.supports_native_streaming = lambda **kw: True

        async def frame(text, *, finalize=False, **kw):
            adapter.frames.append({"text": text, "finalize": finalize})
            return True

        adapter.send_stream_frame = frame
    else:
        adapter._silence_test_transport = "draft"
        adapter.draft_stream_is_message = True
        adapter.supports_draft_streaming = lambda **kw: True

        async def draft(*, content, **kw):
            adapter.frames.append({"text": content, "finalize": False})
            return SendResult(success=True)

        adapter.send_draft = draft


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
async def test_folded_human_gets_one_gateway_retry(harness, marker):
    h = harness
    result = await _run(h, [marker, ANSWER])
    assert result["final_response"] == ANSWER
    assert len(h.calls) == 2
    assert h.event.reply_expected is None
    assert h.calls[1].run_generation == h.calls[0].run_generation == 1
    assert h.calls[1].source is h.calls[0].source
    assert h.calls[1].event_message_id == h.calls[1].inbound_message_id == "typed-1"
    assert h.calls[1].message != RELAY_TEXT and h.calls[1].message != TYPED_TEXT
    assert h.calls[1].persist_user_display_kind == "internal_notification"
    assert any(TYPED_TEXT in str(row.get("content")) for row in h.calls[1].history)
    assert result["history_offset"] == 0
    assert (await _shape(h, result))[:2] == (ANSWER, False)
    assert len(h.consumers) == 2 and h.consumers[0] is not h.consumers[1]
    assert _payloads(h) and all("NO_REPLY" not in payload and "[SILENT]" not in payload for payload in _payloads(h))


@pytest.mark.asyncio
async def test_second_marker_is_terminal_and_human_fallback_remains(harness):
    h = harness
    result = await _run(h, ["NO_REPLY", "NO_REPLY"])
    assert len(h.calls) == 2
    assert h.agent.client.chat.completions.create.call_count == 3
    assert (await _shape(h, result))[:2] == (_unexpected_silence_reply(), False)
    assert not _payloads(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "internal_notification"])
async def test_relay_only_or_machinery_has_no_retry_or_delivery(harness, kind):
    h = harness
    if kind:
        h.event.reply_expected = None
    result = await _run(h, ["NO_REPLY"], typed=False, display_kind=kind)
    assert len(h.calls) == 1
    assert (await _shape(h, result))[1] is True
    assert not _payloads(h)


@pytest.mark.asyncio
async def test_retry_tool_work_does_not_renew_retry_budget(harness):
    h = harness
    tool = _mock_response(content="Checking.", finish_reason="tool_calls",
        tool_calls=[_mock_tool_call(name="web_search", arguments="{}", call_id="retry-tool")])
    result = await _run(h, ["NO_REPLY", tool, "NO_REPLY"])
    assert len(h.calls) == 2
    assert h.agent.client.chat.completions.create.call_count == 4
    assert (await _shape(h, result))[:2] == (_unexpected_silence_reply(), False)
    assert all("NO_REPLY" not in text for text in _payloads(h)), _payloads(h)
    assert {row["tool_call_id"] for row in result["messages"] if row.get("role") == "tool"} == {
        "original-tool", "retry-tool"}
    # A subsequent genuine human request gets its own one-shot gateway retry.
    h.event = MessageEvent(text="another question", source=h.event.source, message_id="human-2")
    h.state.turn.event = h.event
    h.state.persistent.run_generation = 1
    result = await _run(h, ["NO_REPLY", "Second answer."], typed=False)
    assert result["final_response"] == "Second answer."
    assert len(h.calls) == 4


@pytest.mark.asyncio
async def test_edit_transport_keeps_earlier_preamble_when_final_marker_is_rejected(harness):
    h = harness
    result = await _run(h, ["NO_REPLY", ANSWER], preamble=True)
    assert result["final_response"] == ANSWER
    assert any("Checking the brief." in text for text in _payloads(h))
    assert not h.adapter.deleted
    assert all("NO_REPLY" not in text for text in _payloads(h))


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["native", "draft"])
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
async def test_cumulative_preamble_survives_without_marker_frames(harness, monkeypatch, transport, marker):
    h = harness
    _cumulative_adapter(h, transport, monkeypatch)
    result = await _run(h, [marker, ANSWER], preamble=True)
    assert result["final_response"] == ANSWER
    assert len(h.consumers) == 2
    first, retry = h.consumers
    assert first._cumulative_transport() and retry._cumulative_transport()
    assert first._accumulated.strip().startswith("Checking the brief."), (
        first._segment_start, first._accumulated, first._stream_ledger, _payloads(h))
    assert marker not in first._accumulated
    assert not first.final_response_sent and not first.final_content_delivered
    assert any("Checking the brief." in payload for payload in _payloads(h))
    assert any(ANSWER in payload for payload in _payloads(h))
    assert all(marker not in payload for payload in _payloads(h))
    assert (await _shape(h, result))[:2] == (ANSWER, False)


@pytest.mark.asyncio
async def test_retry_guidance_is_api_only_and_real_compaction_keeps_typed_ask(harness, tmp_path):
    from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX
    from agent.conversation_compression import compress_context
    from agent.synthetic_prompt import human_prompt_text
    from gateway.run_turn import _GATEWAY_SILENCE_RETRY_NOTE
    from hermes_state import SessionDB

    h = harness
    result = await _run(h, ["NO_REPLY", ANSWER])
    messages = result["messages"]
    assert h.calls[1].internal is True
    assert h.calls[1].context_prompt == h.calls[0].context_prompt == ""
    wire_calls = h.agent.client.chat.completions.create.call_args_list
    assert _GATEWAY_SILENCE_RETRY_NOTE not in str(wire_calls[0])
    assert _GATEWAY_SILENCE_RETRY_NOTE in str(wire_calls[-1])
    assert _GATEWAY_SILENCE_RETRY_NOTE not in str(messages)
    # The actual compressed-user selector must look past the synthetic empty runtime row.
    compressor = h.agent.context_compressor
    idx = compressor._find_last_user_message_idx(messages, head_end=0)
    assert TYPED_TEXT in human_prompt_text(messages[idx]["content"], display_kind=messages[idx].get("display_kind"))
    assert not ContextCompressor._is_real_user_turn(
        next(row for row in messages if row.get("display_kind") == "internal_notification"))
    with SessionDB(db_path=tmp_path / "silence-state.db") as db:
        db.create_session("silence-test", source="telegram", model="test/model")
        h.agent._session_db = db
        # Use the real durability and in-place compaction transaction boundaries.
        h.agent._flush_messages_to_session_db(messages)
        archived_before = db.get_messages_as_conversation("silence-test")
        assert TYPED_TEXT in str(archived_before)
        assert _GATEWAY_SILENCE_RETRY_NOTE not in str(archived_before)
        h.agent.compression_enabled = True
        h.agent.compression_in_place = True
        compressor.protect_first_n = 0
        compressor.protect_last_n = 2
        compressor.tail_token_budget = 80
        summary = f"{SUMMARY_PREFIX}\n## Historical task\nUser asked about the brief.\n## Completed Actions\nRead the brief."
        padding = [{"role": "assistant", "content": f"Earlier step {i}: " + "x" * 300} for i in range(24)]
        with patch.object(compressor, "_generate_summary", return_value=summary):
            compacted, _ = compress_context(h.agent, padding + messages, "You are helpful.",
                                            approx_tokens=90_000, force=True)
        assert len(compacted) < len(padding + messages)
        retained_idx = compressor._find_last_user_message_idx(compacted, head_end=0)
        assert TYPED_TEXT in str(compacted[retained_idx]["content"])
        assert _GATEWAY_SILENCE_RETRY_NOTE not in str(compacted)
        archived_after = db.get_messages_as_conversation("silence-test")
        assert TYPED_TEXT in str(archived_after)
        assert _GATEWAY_SILENCE_RETRY_NOTE not in str(archived_after)
        # Sidecars, archived rows, and live messages must not hide the note either.
        rows = db._conn.execute("SELECT content, api_content FROM messages").fetchall()
        assert _GATEWAY_SILENCE_RETRY_NOTE not in str(rows)


@pytest.mark.asyncio
async def test_direct_human_text_retry_has_no_original_tool_execution(harness):
    h = harness
    h.event = MessageEvent(text=TYPED_TEXT, source=h.event.source, message_id="human-1")
    h.state.turn.event = h.event
    result = await _run(h, ["NO_REPLY", ANSWER], typed=False, first_tool=False)
    assert len(h.calls) == 2
    assert h.agent.client.chat.completions.create.call_count == 2
    assert not any(row.get("role") == "tool" for row in result["messages"])
    assert result["final_response"] == ANSWER
    assert all("NO_REPLY" not in text for text in _payloads(h)), _payloads(h)


@pytest.mark.asyncio
async def test_folded_human_during_retry_cannot_renew_budget(harness):
    h = harness

    def fold_again(response):
        if len(h.calls) == 2 and response.choices[0].message.content == "NO_REPLY":
            event = MessageEvent(text="and what were the results?", source=h.event.source, message_id="typed-2")
            assert h.runner._fold_into_running_turn(h.agent, SESSION_KEY, event) is h.state.turn
            assert h.calls[1].reply_expected is None

    h.model_hook = fold_again
    result = await _run(h, ["NO_REPLY", "NO_REPLY"])
    assert len(h.calls) == 2
    assert (await _shape(h, result))[:2] == (_unexpected_silence_reply(), False)


@pytest.mark.asyncio
async def test_stale_execution_does_not_retry(harness):
    h = harness

    def invalidate(response):
        if response.choices[0].message.content == "NO_REPLY":
            h.state.persistent.run_generation += 1

    h.model_hook = invalidate
    result = await _run(h, ["NO_REPLY"])
    assert result["final_response"] == "NO_REPLY"
    assert len(h.calls) == 1
    assert not _payloads(h)


@pytest.mark.asyncio
async def test_queued_human_after_retry_gets_fallback_once_and_own_fresh_retry(harness, monkeypatch):
    from gateway.run_turn import _GATEWAY_SILENCE_RETRY_NOTE

    h = harness
    queued = False

    async def drain(result, adapter, source, key):
        nonlocal queued
        if len(h.calls) == 2 and not queued:
            queued = True
            event = MessageEvent(text="A separate queued question.", source=source, message_id="queued-1")
            return event, event.text
        return None, None

    monkeypatch.setattr(h.runner, "_run_agent_drain_pending", drain)
    result = await _run(h, ["NO_REPLY", "NO_REPLY", "NO_REPLY", "Queued answer."])
    assert len(h.calls) == 4
    assert result["final_response"] == "Queued answer."
    assert result["queued_terminal_display_kind"] is None
    assert result["queued_terminal_inbound_id"] == "queued-1"
    assert h.calls[2].silence_retry is False and h.calls[2].context_prompt == ""
    assert h.calls[3].silence_retry is True
    assert _GATEWAY_SILENCE_RETRY_NOTE not in str(h.agent.client.chat.completions.create.call_args_list[-2])
    assert [call["content"] for call in h.adapter.sent].count(_unexpected_silence_reply()) == 1
    assert all("NO_REPLY" not in text for text in _payloads(h))
