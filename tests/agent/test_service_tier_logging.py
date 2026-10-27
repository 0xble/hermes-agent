"""Tier audits use the final request and response echo, including usage-less replies."""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import turn_usage
from agent.chat_completion_helpers_relay import RelayChatAccumulator
from agent.turn_api_call import perform_api_call


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("speed tier:")]


@pytest.mark.parametrize("mode", ["codex_responses", "chat_completions"])
@pytest.mark.parametrize("requested,served,expected", [
    ("ultrafast", "default", "requested=ultrafast served=default"),
    ("priority", None, "requested=priority served=unreported"),
    (None, "priority", "requested=none served=priority"),
    (" PRIORITY ", " ULTRAFAST ", "requested=priority served=ultrafast"),
    (None, None, "requested=none served=unreported"),
    ("", " ", "requested=none served=unreported"),
])
def test_tier_line(mode, requested, served, expected, caplog):
    agent = SimpleNamespace(session_id="child-session", model="configured-model",
                            provider="custom:codex-proxy", api_mode=mode, service_tier="auto")
    with caplog.at_level(logging.INFO):
        turn_usage.log_response_service_tier(
            agent, SimpleNamespace(service_tier=served), {"model": "gpt-6.1-sol", "service_tier": requested})
    if expected is None:
        assert not _lines(caplog)
    else:
        assert _lines(caplog) == [
            "speed tier: session=child-session model=gpt-6.1-sol provider=custom:codex-proxy "
            f"route={mode} {expected}"]


def test_extra_body_matches_sdk_wire_precedence(caplog):
    agent = SimpleNamespace(session_id="s", model="m", provider="openai", api_mode="chat_completions")
    with caplog.at_level(logging.INFO):
        turn_usage.log_response_service_tier(agent, SimpleNamespace(), {
            "service_tier": "ultrafast", "extra_body": {"service_tier": "default"}})
    assert len(_lines(caplog)) == 1
    assert "requested=default served=unreported" in _lines(caplog)[0]


@pytest.mark.parametrize("mode", ["anthropic_messages", "bedrock_converse"])
def test_other_wires_do_not_emit_a_tier_line(mode, caplog):
    with caplog.at_level(logging.INFO):
        turn_usage.log_response_service_tier(SimpleNamespace(api_mode=mode),
                                             SimpleNamespace(service_tier="priority"), {"service_tier": "priority"})
    assert not _lines(caplog)


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("mode", ["codex_responses", "chat_completions"])
@pytest.mark.parametrize("requested,served", [("priority", "default"), (None, None)])
def test_response_is_logged_once_after_middleware_and_preflight(
    mode, streaming, requested, served, monkeypatch, caplog,
):
    response = SimpleNamespace(service_tier=served)
    # Stand-in for the negotiated transport kwargs, not the agent's Ultrafast preference.
    preflight = lambda kwargs, **ignored: {**kwargs, "service_tier": requested}
    agent = SimpleNamespace(
        api_mode=mode, service_tier="ultrafast", model="gpt-6.1-sol", provider="custom:codex-proxy",
        session_id="child-session", platform="subagent", base_url="http://127.0.0.1:8317/v1",
        _get_transport=lambda: SimpleNamespace(preflight_kwargs=preflight),
        _is_copilot_url=lambda: False, _is_codex_backend=lambda: False,
        _interruptible_api_call=lambda kwargs: response,
        _interruptible_streaming_api_call=lambda kwargs, **ignored: response,
        _has_pending_redirect=lambda: False,
    )
    monkeypatch.setattr("agent.turn_api_call._should_stream", lambda agent: streaming)
    monkeypatch.setattr("hermes_cli.middleware.run_llm_execution_middleware",
                        lambda kwargs, invoke, **ignored: invoke({**kwargs, "service_tier": requested}))
    monkeypatch.setattr("agent.relay_llm.execute", lambda kwargs, invoke, **ignored: invoke(kwargs))
    with caplog.at_level(logging.INFO):
        verdict = perform_api_call(
            agent, api_kwargs={"service_tier": "ultrafast"}, _original_api_kwargs={}, _llm_middleware_trace=[],
            _moa_prepared_request=None, _retry=None, thinking_spinner=None, retry_count=0,
            api_call_count=1, api_request_id="request", effective_task_id="task", turn_id="turn", interrupted=False)
    assert verdict.response is response
    assert len(_lines(caplog)) == 1
    assert f"requested={requested or 'none'} served={served or 'unreported'}" in _lines(caplog)[0]
    assert "session=child-session" in _lines(caplog)[0]


@pytest.mark.parametrize("mode", ["chat_completions", "codex_responses"])
@pytest.mark.parametrize("execution", ["sync", "stream", "nested_stream"])
def test_requested_tier_matches_real_relay_provider_callback(mode, execution, relay_turn, monkeypatch, caplog):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from agent import relay_llm

    relay, _turn = relay_turn
    sent_tiers = []
    response = SimpleNamespace(service_tier="default", model="gpt-6.1-sol", choices=[])

    def rewrite_tier(name, request, annotated):
        annotated.service_tier = "default"
        return relay.LLMRequestInterceptOutcome(request, annotated)

    def provider(kwargs):
        sent_tiers.append(kwargs.get("service_tier"))
        return response

    def stream_provider(kwargs):
        sent_tiers.append(kwargs.get("service_tier"))
        if mode == "codex_responses":
            return iter([{"type": "response.completed", "response": {
                "id": "response-1", "status": "completed", "model": "gpt-6.1-sol",
                "service_tier": "default", "output": [],
            }}])
        return iter([{"choices": [], "service_tier": "default"}])

    def consume_stream(kwargs, **ignored):
        stream = relay_llm.stream(
            kwargs, stream_provider, session_id="session-1", name="custom:codex-proxy",
            model_name="gpt-6.1-sol", metadata={"api_mode": mode}, finalizer=lambda: response,
        )
        try:
            list(stream)
        finally:
            stream.close()
        return response

    def threaded_stream(kwargs, **ignored):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(copy_context().run, consume_stream, kwargs).result()

    def nested_stream(kwargs):
        # Codex opens another Relay stream inside the outer non-stream callback.
        return consume_stream({**kwargs, "service_tier": "priority"})

    agent = SimpleNamespace(
        api_mode=mode, model="gpt-6.1-sol", provider="custom:codex-proxy",
        session_id="session-1", platform="subagent", base_url="http://provider.invalid/v1",
        _get_transport=lambda: SimpleNamespace(preflight_kwargs=lambda kwargs, **ignored: kwargs),
        _is_copilot_url=lambda: False, _is_codex_backend=lambda: False,
        _interruptible_api_call=nested_stream if execution == "nested_stream" else provider,
        _interruptible_streaming_api_call=threaded_stream, _has_pending_redirect=lambda: False,
    )
    monkeypatch.setattr("agent.turn_api_call._should_stream", lambda agent: execution == "stream")
    relay.intercepts.register_llm_request("tier-audit-rewrite", 1, False, rewrite_tier)
    try:
        with caplog.at_level(logging.INFO):
            verdict = perform_api_call(
                agent, api_kwargs={"model": agent.model, "messages": [], "input": [],
                                   "service_tier": "ultrafast"},
                _original_api_kwargs={}, _llm_middleware_trace=[], _moa_prepared_request=None,
                _retry=None, thinking_spinner=None, retry_count=0, api_call_count=1,
                api_request_id="tier-audit-request", effective_task_id="task-1",
                turn_id="turn-1", interrupted=False)
    finally:
        relay.intercepts.deregister_llm_request("tier-audit-rewrite")
    assert verdict.response is response
    expected = "priority" if execution == "nested_stream" else "default"
    assert sent_tiers == [expected]
    assert len(_lines(caplog)) == 1
    assert f"requested={expected} served=default" in _lines(caplog)[0]


def test_chat_relay_keeps_tier_on_choiceless_usage_frame():
    accumulator = RelayChatAccumulator()
    accumulator.observe({"model": "gpt-6.1-sol", "choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]})
    accumulator.observe({"choices": [], "service_tier": "priority", "usage": {"completion_tokens": 1}})
    accumulator.observe({"choices": [], "service_tier": None})
    assert accumulator.finalize()["service_tier"] == "priority"


# Reuse the real conversation/stream harness, not a fabricated completed response.
from tests.agent import test_first_chunk_at_hook, test_relay_llm
from tests.agent.test_first_chunk_at_hook import _make_stream_chunk, _run_with_hooks

agent = test_first_chunk_at_hook.agent
relay_turn = test_relay_llm.relay_turn


def test_streamed_chat_tier_reaches_the_conversation_log(agent, caplog):
    from unittest.mock import MagicMock

    chunks = [_make_stream_chunk(content="OK", finish_reason="stop", model="test-model")]
    chunks[0].service_tier = "default"
    client = MagicMock()
    client.chat.completions.create.return_value = iter(chunks)
    agent.stream_delta_callback = lambda text: None
    agent.request_overrides = {"service_tier": "priority"}
    with patch.object(agent, "_create_request_openai_client", return_value=client), \
         patch.object(agent, "_close_request_openai_client"), caplog.at_level(logging.INFO):
        result, _post = _run_with_hooks(agent)
    assert result["final_response"] == "OK"
    assert len(_lines(caplog)) == 1
    assert "requested=priority served=default" in _lines(caplog)[0]
