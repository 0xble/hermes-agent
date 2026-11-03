"""Gateway-owned recovery: a folded human ask gets one fresh execution, not an agent-loop nudge."""
from __future__ import annotations

import threading
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
        "model": {"default": "test/model", "provider": "openrouter"},
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
        agent = AIAgent(model="test/model", api_key="test-key-1234567890",
                        base_url="https://openrouter.ai/api/v1", quiet_mode=True,
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
    # This resolver double reuses one agent, so retain its cache ownership like
    # the production resolver. An uncached agent is retired after the outer turn.
    runner._agent_cache = {SESSION_KEY: (agent, "silence-test", 0, "silence-test")}
    runner._draining = False
    state = runner._session_state(SESSION_KEY)
    state.persistent.run_generation = 1
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345",
                           thread_id="212659")
    assert runner._session_key_for_source(source) == SESSION_KEY
    event = MessageEvent(text=RELAY_TEXT, source=source, message_id="relay-1", reply_expected=False)
    state.turn.event = event
    calls, consumers, preview_ticks = [], [], []
    tick_done = threading.Event()
    delta_pending = threading.Event()
    fire_delta = agent._fire_stream_delta

    def fire_and_tick(text):
        # Hold the provider worker until the real consumer has considered this delta.
        # Force the edit timer due, rather than hoping a wall-clock sleep hits a tick.
        if not consumers or not consumers[-1]._run_still_current():
            fire_delta(text)
            return
        tick_done.clear()
        consumers[-1]._last_edit_time = 0.0
        fire_delta(text)
        delta_pending.set()
        assert tick_done.wait(5.0), "stream consumer did not process the delta"

    monkeypatch.setattr(agent, "_fire_stream_delta", fire_and_tick)

    def resolve(turn_runner, route, platform_key, ephemeral, *args):
        ctx = turn_runner._ctx
        ctx.bind_agent_lifetime(agent)
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
        should_edit = consumer._should_edit

        def observe_tick(tick):
            result = should_edit(tick)
            if delta_pending.is_set() and consumer._queue.empty():
                preview_ticks.append((consumer._accumulated, result))
                delta_pending.clear()
                tick_done.set()
            return result

        consumer._should_edit = observe_tick
        return consumer

    monkeypatch.setattr(stream_mod, "GatewayStreamConsumer", capture_consumer)
    return SimpleNamespace(agent=agent, runner=runner, adapter=adapter, event=event,
                           state=state, calls=calls, consumers=consumers, preview_ticks=preview_ticks)


async def _run(h, finals, *, typed=True, preamble=False, display_kind=None, first_tool=True,
               fragmented=False):
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
        if not kwargs.get("stream"):
            return response
        # Return an actual provider iterator. Previously this stub emitted deltas
        # AND returned a completed object to the streaming parser; its fallback
        # replayed the content, making NO_REPLYNO_REPLY an ordinary visible segment.
        message = response.choices[0].message
        tools = [SimpleNamespace(index=i, id=tool.id, function=tool.function)
                 for i, tool in enumerate(message.tool_calls or [])] or None
        parts = [message.content]
        if fragmented and message.content == "NO_REPLY":
            parts = ["NO_", "REPLY"]
        chunks = [SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=part, tool_calls=tools if i == 0 else None),
            finish_reason=None)]) for i, part in enumerate(parts)]
        chunks.append(SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=None),
            finish_reason=response.choices[0].finish_reason)]))
        return iter(chunks)

    h.agent.client.chat.completions.create.side_effect = model
    h.agent._interruptible_api_call = lambda kw: h.agent.client.chat.completions.create(**kw)
    return await h.runner._run_agent(message=h.event.text, context_prompt="", history=[],
        source=h.event.source, session_id="silence-test", session_key=SESSION_KEY,
        run_generation=1, reply_expected=h.event.reply_expected,
        persist_user_display_kind=display_kind, event_message_id="relay-1", inbound_message_id="relay-1")


async def _shape(h, result):
    return await h.runner._hmwa_shape_agent_response(result, h.event.source, history=[],
        session_entry=SimpleNamespace(session_id=result.get("session_id"), incognito=False), session_key=None,
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

        async def frame(text, *, turn_id, finalize=False, **kw):
            adapter.frames.append({"text": text, "finalize": finalize, "turn_id": turn_id})
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
async def test_retry_preserves_redirected_terminal_ledger_identity(harness):
    h = harness
    redirect_event = MessageEvent(
        text="the newer typed question", source=h.event.source, message_id="typed-2",
    )
    h.agent.redirect = MagicMock(return_value=True)
    redirected = False

    def redirect_during_retry(_response):
        nonlocal redirected
        if len(h.calls) == 2 and not redirected:
            redirected = True
            assert h.runner._redirect_active_turn(
                h.agent, redirect_event.text, SESSION_KEY, redirect_event,
            )

    h.model_hook = redirect_during_retry
    result = await _run(h, ["NO_REPLY", ANSWER])

    assert redirected
    assert result["final_response"] == ANSWER
    assert result["queued_terminal_inbound_id"] == "typed-2"
    assert result["_terminal_inbound_id"] == "typed-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
async def test_proxy_silence_keeps_fallback_without_retry(harness, monkeypatch, marker):
    """Real proxy route needs a new non-empty conversation row; a system note is not one."""
    from unittest.mock import AsyncMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    h = harness
    api = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    monkeypatch.setattr(api, "_ensure_session_db", lambda: None)
    remote_run = AsyncMock(return_value=({"final_response": marker, "completed": True}, {}))
    monkeypatch.setattr(api, "_run_agent", remote_run)
    app = web.Application()
    app["api_server_adapter"] = api
    app.router.add_post("/v1/chat/completions", api._handle_chat_completions)
    note = {"role": "system", "content": "Answer the latest human request; do not return a silence marker."}
    async with TestClient(TestServer(app)) as client:
        # Removing the empty runtime row leaves only a system note: rejected. Retaining the
        # silent result as the last row would instead promote that assistant marker to user
        # input (the route always executes its final non-system row), not continue it.
        for messages in ([note], [note, {"role": "user", "content": ""}]):
            rejected = await client.post("/v1/chat/completions", json={"messages": messages})
            assert rejected.status == 400
            assert "No user message found" in (await rejected.json())["error"]["message"]
        assert remote_run.await_count == 0
        system_last = await client.post("/v1/chat/completions", json={"messages": [
            {"role": "user", "content": TYPED_TEXT}, {"role": "assistant", "content": marker}, note,
        ]})
        assert system_last.status == 200
        assert remote_run.await_args.kwargs["user_message"] == marker
        assert remote_run.await_args.kwargs["conversation_history"] == [{"role": "user", "content": TYPED_TEXT}]
        assert remote_run.await_args.kwargs["ephemeral_system_prompt"] == note["content"]
        remote_run.reset_mock()

        monkeypatch.setattr(h.runner, "_get_proxy_url", lambda: str(client.make_url("")).rstrip("/"))
        result = await h.runner._run_agent(
            message=TYPED_TEXT, context_prompt="", history=[], source=h.event.source,
            session_id="", session_key=SESSION_KEY, run_generation=1,
            reply_expected=None, event_message_id="typed-1", inbound_message_id="typed-1",
        )
    assert remote_run.await_count == 1
    assert remote_run.await_args.kwargs["user_message"] == TYPED_TEXT
    assert remote_run.await_args.kwargs["ephemeral_system_prompt"] is None
    assert result["final_response"] == marker
    shaped = await h.runner._hmwa_shape_agent_response(
        result, h.event.source, history=[], session_entry=SimpleNamespace(session_id="silence-test", incognito=False),
        session_key=None, _quick_key=SESSION_KEY, run_generation=1, _run_start_session_id="silence-test",
        _platform_name="telegram", _msg_start_time=0.0, reply_expected=None, persist_user_display_kind=None,
    )
    assert shaped[:2] == (_unexpected_silence_reply(), False)
    assert not h.calls  # no local execution/retry was substituted for the proxy turn
    assert all(marker not in payload for payload in _payloads(h))


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

    native_at_retry = []

    def check_retry_start(response):
        if transport == "native" and len(h.calls) == 2:
            first = h.consumers[0]
            # The retry may already have seeded its own native stream.
            last_frame = next(frame for frame in reversed(h.adapter.frames)
                              if frame["turn_id"] == first._turn_id)
            native_at_retry.append((
                first._native_stream_opened, last_frame.copy(),
                first._accumulated, first.final_content_delivered,
            ))

    h.model_hook = check_retry_start
    result = await _run(h, [marker, ANSWER], preamble=True)
    assert result["final_response"] == ANSWER
    if transport == "native":
        assert len(native_at_retry) == 1
        opened, frame, prefix, delivered = native_at_retry[0]
        assert not opened
        assert frame == {"text": prefix, "finalize": True,
                         "turn_id": h.consumers[0]._turn_id}
        assert prefix.startswith("Checking the brief.")
        assert not delivered
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
    retry_wire_messages = wire_calls[-1].kwargs["messages"]
    assert retry_wire_messages[-1]["role"] in {"user", "assistant"}
    wire_cue = retry_wire_messages[-1]["content"]
    assert wire_cue.strip()
    for ephemeral in (_GATEWAY_SILENCE_RETRY_NOTE, wire_cue):
        assert ephemeral not in str(messages)
    assert not any(
        row.get("display_kind") == "internal_notification" and row.get("content")
        for row in messages
    )
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
        for ephemeral in (_GATEWAY_SILENCE_RETRY_NOTE, wire_cue):
            assert ephemeral not in str(archived_before)
        assert not any(
            row.get("display_kind") == "internal_notification" and row.get("content")
            for row in archived_before
        )
        h.agent.compression_enabled = True
        h.agent.compression_in_place = True
        compressor.protect_first_n = 0
        compressor.protect_last_n = 2
        compressor.tail_token_budget = 80
        summary = f"{SUMMARY_PREFIX}\n## Historical task\nUser asked about the brief.\n## Completed Actions\nRead the brief."
        padding = [{"role": "assistant", "content": f"Earlier step {i}: " + "x" * 300} for i in range(24)]
        with patch.object(compressor, "_generate_summary", return_value=summary) as summarize:
            compacted, _ = compress_context(h.agent, padding + messages, "You are helpful.",
                                            approx_tokens=90_000, force=True)
        assert summarize.called
        for ephemeral in (_GATEWAY_SILENCE_RETRY_NOTE, wire_cue):
            assert ephemeral not in str(summarize.call_args_list)
        assert len(compacted) < len(padding + messages)
        retained_idx = compressor._find_last_user_message_idx(compacted, head_end=0)
        assert retained_idx >= 0
        assert TYPED_TEXT in str(compacted[retained_idx]["content"])
        assert _GATEWAY_SILENCE_RETRY_NOTE not in str(compacted)
        archived_after = db.get_messages_as_conversation("silence-test")
        assert TYPED_TEXT in str(archived_after)
        assert _GATEWAY_SILENCE_RETRY_NOTE not in str(archived_after)
        # Sidecars, archived rows, and live messages must not hide the note either.
        rows = db._conn.execute("SELECT content, api_content FROM messages").fetchall()
        for ephemeral in (_GATEWAY_SILENCE_RETRY_NOTE, wire_cue):
            assert ephemeral not in str(compacted)
            assert ephemeral not in str(archived_after)
            assert ephemeral not in str(rows)


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
    assert h.preview_ticks == [("NO_REPLY", False), (ANSWER, True)]
    assert all("NO_REPLY" not in text for text in _payloads(h)), _payloads(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["edit", "native", "draft"])
@pytest.mark.parametrize("first_tool", [False, True])
@pytest.mark.parametrize("retry_marker", [False, True])
async def test_fragmented_marker_is_held_at_each_due_preview_tick(
    harness, monkeypatch, transport, first_tool, retry_marker,
):
    h = harness
    if not first_tool:
        h.event = MessageEvent(text=TYPED_TEXT, source=h.event.source, message_id="human-1")
        h.state.turn.event = h.event
    if transport != "edit":
        _cumulative_adapter(h, transport, monkeypatch)
    result = await _run(h, ["NO_REPLY", "NO_REPLY" if retry_marker else ANSWER],
                        typed=first_tool, first_tool=first_tool, fragmented=True)
    assert len(h.calls) == 2
    assert result["final_response"] == ("NO_REPLY" if retry_marker else ANSWER)
    marker_ticks = [(text.strip(), edit) for text, edit in h.preview_ticks if text.strip().startswith("NO_")]
    assert marker_ticks == [("NO_", False), ("NO_REPLY", False)] * (2 if retry_marker else 1)
    assert all("NO_" not in text for text in _payloads(h)), _payloads(h)
    assert (await _shape(h, result))[:2] == (
        _unexpected_silence_reply() if retry_marker else ANSWER, False,
    )


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
async def test_interrupted_partial_marker_does_not_retry(harness):
    """A provider interrupt preserves streamed partial text as final_response; a partial
    NO_REPLY is not a completed silence choice, so it must never restart execution."""
    h = harness
    # A human-addressed turn: relay-only silence is allowed and would never re-prompt.
    h.event = MessageEvent(text=TYPED_TEXT, source=h.event.source, message_id="human-1")
    h.state.turn.event = h.event
    first = True

    def interrupt_after_partial_marker(response):
        nonlocal first
        if first:
            first = False
            h.agent._current_streamed_assistant_text = "NO_REPLY"
            raise InterruptedError("provider request interrupted after partial marker")

    h.model_hook = interrupt_after_partial_marker
    executions = []
    original = h.agent.run_conversation

    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        executions.append(result)
        return result

    h.agent.run_conversation = run
    result = await _run(h, ["NO_REPLY", ANSWER], typed=False, first_tool=False)
    assert executions and executions[0].get("interrupted") is True
    assert [ctx.silence_retry for ctx in h.calls] == [False], (
        "an interrupted partial marker started a silence re-prompt")
    assert ANSWER not in str(result.get("final_response"))


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
