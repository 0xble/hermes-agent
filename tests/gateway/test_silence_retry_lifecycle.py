"""Recovery must preserve real agent-cache identity and intermediate stream ownership.

Drive GatewayRunner with the real prompt composer, cache resolver, constructor and
agent request builder (only model/transport I/O is fake). Stream checks drive the
consumer queue through explicit flush barriers, not sleeps or suppression helpers.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run_turn import _GATEWAY_SILENCE_RETRY_NOTE
from gateway.run_turn_runner import TurnRunner
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from tests.agent.test_run_agent import _mock_response
from tests.gateway import test_folded_steer_silence_reprompt as _folded
from tests.gateway.test_folded_steer_silence_reprompt import SESSION_KEY, _cumulative_adapter

# Share the integration fixture by module binding; a function parameter then is not an import shadow.
harness = _folded.harness

# Save before the older integration fixture installs its resolver double.
_REAL_RESOLVER = TurnRunner._resolve_turn_agent


@pytest.fixture
def cached_harness(harness, monkeypatch, tmp_path):
    """Retain the real cache/constructor boundary the earlier fixture bypasses."""
    import hermes_yaml as yaml
    from run_agent import AIAgent

    h = harness
    monkeypatch.setattr(TurnRunner, "_resolve_turn_agent", _REAL_RESOLVER)
    h.runner._agent_cache = {}
    h.runner._agent_cache_lock = threading.Lock()
    # Use the real runtime/model resolver and a coherent config. A resolver
    # double returning test/model with no model.default triggers real fallback
    # eviction after every success, independently of the silence-retry cache key.
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "openrouter", "api_key": "test-key-1234567890", "base_url": "https://openrouter.ai/api/v1",
    })
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    config["model"] = {"default": "test/model", "provider": "openrouter"}
    config["agent"] = {"system_prompt": "Gateway context."}
    config["streaming"]["enabled"] = False
    config["gateway"] = {"platforms": {"telegram": {"skip_context_files": True}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *a, **kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *a, **kw: {})
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", MagicMock())
    h.constructed, h.executions, h.requests = [], [], []
    shared_client = h.agent.client
    original_init = AIAgent.__init__
    original_run = AIAgent.run_conversation

    def initialize(agent, *args, **kwargs):
        kwargs["skip_memory"] = True
        original_init(agent, *args, **kwargs)
        h.constructed.append(agent)
        agent.client = shared_client
        agent._cached_system_prompt = "Frozen conversation prompt."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent._interruptible_api_call = lambda kw: shared_client.chat.completions.create(**kw)
        for name in ("_persist_session", "_save_trajectory", "_cleanup_task_resources"):
            monkeypatch.setattr(agent, name, MagicMock())

    def execute(agent, *args, **kwargs):
        h.requests.append((agent, agent.ephemeral_system_prompt, agent._cached_system_prompt, agent.tools))
        return original_run(agent, *args, **kwargs)

    original_wire = TurnRunner._wire_turn_agent_callbacks

    def wire(turn, agent, *args):
        h.executions.append((turn._ctx, agent))
        return original_wire(turn, agent, *args)

    monkeypatch.setattr(AIAgent, "__init__", initialize)
    monkeypatch.setattr(AIAgent, "run_conversation", execute)
    monkeypatch.setattr(TurnRunner, "_wire_turn_agent_callbacks", wire)
    h.event = MessageEvent(text="A human question.", source=h.event.source, message_id="human-1")
    h.state.turn.event = h.event
    return h


async def _human_turn(h, finals, *, history=None):
    h.agent.client.chat.completions.create.side_effect = [
        _mock_response(content=text, finish_reason="stop") for text in finals
    ]
    return await h.runner._run_agent(
        message=h.event.text, context_prompt="Platform context.", channel_prompt="Channel context.",
        history=history or [], source=h.event.source, session_id="silence-test", session_key=SESSION_KEY,
        run_generation=1, event_message_id=h.event.message_id, inbound_message_id=h.event.message_id,
    )


def _assert_restored(h):
    assert len(h.constructed) == 1, "normal → recovery → normal rebuilt the cached agent"
    agent = h.constructed[0]
    assert all(executed is agent for _, executed in h.executions)
    assert h.runner._agent_cache[SESSION_KEY][0] is agent
    assert agent.ephemeral_system_prompt == "Platform context.\n\nChannel context.\n\nGateway context."
    assert agent._cached_system_prompt == "Frozen conversation prompt."
    assert not hasattr(agent, "_gateway_silence_retry_wire_message")
    assert all(tools is agent.tools for _, _, _, tools in h.requests)
    assert all(ctx.source is h.event.source for ctx, _ in h.executions)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
async def test_recovery_and_next_human_reuse_real_cache(cached_harness, marker):
    h = cached_harness
    result = await _human_turn(h, [marker, "Recovered."])
    assert result["final_response"] == "Recovered."
    assert [ctx.silence_retry for ctx, _ in h.executions] == [False, True]
    # Snapshot actual model payloads before a subsequent call replaces the fake responses.
    calls = h.agent.client.chat.completions.create.call_args_list
    assert _GATEWAY_SILENCE_RETRY_NOTE not in str(calls[0])
    assert _GATEWAY_SILENCE_RETRY_NOTE in str(calls[1])
    retry_final = calls[1].kwargs["messages"][-1]
    assert retry_final["role"] == "user" and retry_final["content"].strip()
    assert retry_final["content"] not in str(result["messages"])
    assert _GATEWAY_SILENCE_RETRY_NOTE not in str(result["messages"])
    # Drive the whole normal → recovery → normal cycle before checking construction count.
    result = await _human_turn(h, ["Next answer."], history=result["messages"])
    assert result["final_response"] == "Next answer."
    assert _GATEWAY_SILENCE_RETRY_NOTE not in str(h.agent.client.chat.completions.create.call_args_list[-1])
    assert retry_final["content"] not in str(h.agent.client.chat.completions.create.call_args_list[-1])
    assert [ctx.silence_retry for ctx, _ in h.executions] == [False, True, False]
    _assert_restored(h)


@pytest.mark.parametrize(("retry", "kind", "content", "expect_cue"), [
    (True, "internal_notification", "", True),
    (False, "internal_notification", "", False),
    (True, None, "", False),
    (True, "internal_notification", "A folded human request.", False),
    (False, None, "A normal human request.", False),
])
def test_wire_cue_is_scoped_to_empty_internal_recovery(harness, retry, kind, content, expect_cue):
    from copy import deepcopy
    from agent.turn_context import build_api_messages
    from gateway.turn_context import TurnContext, silence_retry_prompt

    agent = harness.agent
    agent._current_turn_timestamp = 1.0
    messages = [{"role": "assistant", "content": "NO_REPLY"},
                {"role": "user", "content": content}]
    if kind:
        messages[-1]["display_kind"] = kind
    frozen = deepcopy(messages)
    with silence_retry_prompt(agent, TurnContext(silence_retry=retry)):
        wire, _ = build_api_messages(
            agent, messages, current_turn_user_idx=1, ext_prefetch_cache="",
            plugin_user_context="", moa_config=None, active_system_prompt="Fixed system prompt.",
        )
        assert wire[-1]["role"] == "user"
        if expect_cue:
            assert wire[-1]["content"].strip() and wire[-1]["content"] != content
        else:
            assert wire[-1]["content"] == content
    assert messages == frozen
    assert not hasattr(agent, "_gateway_silence_retry_wire_message")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed_result", "exception", "approval_exception"])
async def test_recovery_failure_restores_cached_prompt(cached_harness, monkeypatch, failure):
    h = cached_harness
    from run_agent import AIAgent

    import tools.approval as approval

    original_run = AIAgent.run_conversation

    def execute(agent, *args, **kwargs):
        if h.executions[-1][0].silence_retry:
            assert _GATEWAY_SILENCE_RETRY_NOTE in agent.ephemeral_system_prompt
            if failure == "approval_exception":
                # Invoke the callback registered by the real request scope, not a copied owner.
                callback = approval._gateway_notify_cbs[SESSION_KEY]
                assert callable(callback)
                callback({})
            if failure != "failed_result":
                raise RuntimeError("recovery execution failed")
            return {"failed": True, "final_response": "Recovery failed.", "messages": kwargs["conversation_history"]}
        return original_run(agent, *args, **kwargs)

    def approval_failure(turn, *args, **kwargs):
        raise RuntimeError("approval failed")

    monkeypatch.setattr(TurnRunner, "_approval_notify_sync", approval_failure)
    monkeypatch.setattr(AIAgent, "run_conversation", execute)
    if failure == "failed_result":
        await _human_turn(h, ["NO_REPLY"])
    else:
        # _run_agent propagates executor exceptions to its caller; restoration must still hold.
        with pytest.raises(RuntimeError):
            await _human_turn(h, ["NO_REPLY"])
    assert SESSION_KEY not in approval._gateway_notify_cbs
    _assert_restored(h)
    # Remove the failure injection, but leave the real cache alive for the next human.
    monkeypatch.setattr(AIAgent, "run_conversation", original_run)
    result = await _human_turn(h, ["Next answer."])
    assert result["final_response"] == "Next answer."
    assert _GATEWAY_SILENCE_RETRY_NOTE not in str(h.agent.client.chat.completions.create.call_args_list[-1])
    _assert_restored(h)


@pytest.mark.asyncio
@pytest.mark.parametrize(("transport", "whole_final"), [
    ("native", False), ("native", True), ("draft", False), ("draft", True), ("edit", False),
])
@pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
async def test_intermediate_marker_preserves_live_cumulative_bubble(harness, monkeypatch, transport, whole_final, marker):
    h = harness
    if transport != "edit":
        _cumulative_adapter(h, transport, monkeypatch)
    cfg = StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, transport=transport)
    consumer = GatewayStreamConsumer(h.adapter, "-1001", cfg)
    task = asyncio.create_task(consumer.run())

    async def flush():
        # The real queue barrier covers all preceding deltas/boundaries.
        assert await asyncio.to_thread(consumer.flush_pending_sync, 5.0)

    try:
        consumer.on_delta("Checking.")
        consumer.on_segment_break()
        await flush()
        consumer.on_delta(marker)
        consumer.on_segment_break()
        await flush()
        if transport == "native":
            assert consumer._native_stream_opened, "intermediate marker prematurely closed the bubble"
            assert not any(frame["finalize"] for frame in h.adapter.frames)
        elif transport == "draft":
            assert consumer._accumulated.strip() == "Checking."
        # Support both a final containing only the last assistant's answer and
        # an already-cumulative final, without dropping or duplicating preambles.
        final_text = consumer._accumulated + "Answer." if whole_final else "Answer."
        consumer.on_delta("Answer.")
        consumer.finish(final_text)
        await asyncio.wait_for(task, 5.0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    payloads = [c["content"] for c in h.adapter.sent + h.adapter.edits]
    payloads += [f["text"] for f in getattr(h.adapter, "frames", [])]
    assert all(marker not in text for text in payloads)
    if transport in {"native", "draft"}:
        closed = ([frame["text"] for frame in h.adapter.frames if frame["finalize"]]
                  if transport == "native" else [call["content"] for call in h.adapter.sent])
        assert len(closed) == 1 and closed[0].count("Checking.") == 1
        assert "Answer." in closed[0]
        # Cumulative preambles must not cause a corrective duplicate final-send.
        assert consumer.delivered_final_matches("Answer.") is True
        assert consumer.delivered_final_matches(closed[0]) is True
        assert consumer.delivered_final_matches("Answer. with an unseen tail") is False
    assert consumer.final_content_delivered
