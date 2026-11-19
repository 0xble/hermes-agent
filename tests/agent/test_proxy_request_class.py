"""Wire-level coverage using SDK clients pointed at in-process HTTP transports."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from agent import auxiliary_client as aux
from agent.proxy_request_class import HEADER, apply_turn_request_class, protect_request_class_redirects
from hermes_cli.runtime_provider import frozen_runtime_config


PROXY = "https://proxy.example.test/v1"
NAMED = "custom:openai-proxy"
MODEL = "test-model"
MESSAGES = [{"role": "user", "content": "Hello"}]


def _config(*, opt_in=True, mode="chat_completions"):
    return {"providers": {
        "openai-proxy": {"api": PROXY, "api_key": "test", "model": MODEL, "api_mode": mode,
                         "hermes_request_class": opt_in, "extra_headers": {"X-Proxy-Option": "kept"}},
        "other": {"api": PROXY, "api_key": "test", "model": MODEL},
    }}


def _chat_response(request):
    return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1,
        "model": MODEL, "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": "ok"}}]})


def test_queued_steer_survives_conversation_start_reset(monkeypatch):
    """A steer accepted before agent adoption must promote the first provider call."""
    from agent.conversation_loop import run_conversation
    from agent.proxy_request_class import turn_request_class

    agent = SimpleNamespace(_pending_steer="human correction", _turn_user_intervened=False)

    def fake_turn(*_args, **kwargs):
        assert kwargs["turn_origin"] == "goal_loop"
        assert turn_request_class(agent) == "interactive"
        return {"final_response": "ok", "messages": []}

    monkeypatch.setattr("agent.conversation_loop._run_conversation_turn", fake_turn)
    monkeypatch.setattr("agent.turn_context.export_current_turn_boundary", lambda *_a: _a[1])
    result = run_conversation(agent, "continue", turn_origin="goal_loop")

    assert result["user_intervened"] is True
    assert agent._turn_user_intervened is True


def test_steer_source_controls_turn_pacing():
    from tests.agent.test_steer import _bare_agent

    agent = _bare_agent()
    agent._turn_user_intervened = False
    assert agent.steer("model note", human=False)
    assert agent._turn_user_intervened is False
    assert agent.steer("human note", human=True)
    assert agent._turn_user_intervened is True
@pytest.mark.parametrize(("origin", "expected"), [("user", "interactive"), ("goal_loop", "background")])
def test_detached_hygiene_executor_binds_origin_on_wire(monkeypatch, origin, expected):
    from gateway.run import GatewayRunner

    sent = []
    def handle(request):
        sent.append(request.headers.get(HEADER))
        return _chat_response(request)

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *_a, **_kw: {
        "http_client": httpx.Client(transport=httpx.MockTransport(handle)),
    })

    def compress(messages, system_message, **_kwargs):
        aux.call_llm(task="compression", provider=NAMED, model=MODEL, messages=MESSAGES)
        return messages, system_message

    agent = SimpleNamespace(
        context_compressor=None, _codex_session=object(), _compress_context=compress,
    )

    async def drive():
        async def build(*_args):
            return agent, None
        async def wait(attempt, *_args):
            return await attempt.future
        async def apply(*_args, **_kwargs):
            return None
        async def cleanup(*_args, **_kwargs):
            return None
        gateway = SimpleNamespace(
            _hmwa_hygiene_build_agent=build, _hmwa_hygiene_wait_for_summary=wait,
            _hmwa_hygiene_apply_result=apply, _track_deferred_agent_worker=lambda *_a: None,
            _evict_cached_agent=lambda _key: None, _cleanup_agent_resources_off_loop=cleanup,
        )
        attempt = GatewayRunner._HygieneAttempt(agent=None, meta=None)
        await GatewayRunner._hmwa_hygiene_detached_attempt(
            gateway, attempt, SimpleNamespace(total_ceiling_seconds=60.0),
            SimpleNamespace(approx_tokens=100_000), MESSAGES, MESSAGES, MODEL, {}, None,
            SimpleNamespace(session_id="sess-1"), "key", "qk", 0, origin,
        )

    with frozen_runtime_config(_config()):
        asyncio.run(drive())
    assert sent == [expected]


@pytest.mark.parametrize(("origin", "expected"), [("user", "interactive"), ("goal_loop", "background")])
def test_hygiene_binds_the_live_turn_origin(origin, expected):
    from agent.proxy_request_class import apply_aux_request_class, bind_aux_request_origin

    client = SimpleNamespace(base_url=PROXY)
    with frozen_runtime_config(_config()):
        with bind_aux_request_origin(origin):
            result = apply_aux_request_class(
                client, {"model": MODEL}, NAMED, "chat_completions", "compression",
            )
    assert result["extra_headers"][HEADER] == expected

def test_title_generation_stays_background_in_interactive_turn_on_wire(monkeypatch):
    from contextvars import copy_context

    from agent.proxy_request_class import bind_aux_request_turn
    from agent.title_generator import generate_title

    sent = []

    def handle(request):
        sent.append(request.headers.get(HEADER))
        return _chat_response(request)

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *_a, **_kw: {
        "http_client": httpx.Client(transport=httpx.MockTransport(handle)),
    })
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: (
        {"provider": NAMED, "model": MODEL} if task == "title_generation" else {}
    ))
    agent = SimpleNamespace(api_mode="chat_completions", provider=NAMED, base_url=PROXY,
                            _turn_provenance="user")
    with frozen_runtime_config(_config()):
        with bind_aux_request_turn(agent):
            # Deferred upgrades capture the turn context and use it after the reply.
            title_context = copy_context()
            with OpenAI(api_key="test", base_url=PROXY,
                        http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
                client.chat.completions.create(**apply_turn_request_class(agent, {
                    "model": MODEL, "messages": MESSAGES,
                }))
            aux.call_llm(task="compression", provider=NAMED, model=MODEL, messages=MESSAGES)
        title_context.run(generate_title, "Investigate queue latency")
        aux.call_llm(task="compression", provider=NAMED, model=MODEL, messages=MESSAGES)
    assert sent == ["interactive", "interactive", "background", "background"]


def test_agent_turn_and_route_change_on_wire(monkeypatch):
    from run_agent import AIAgent

    sent = []

    def handle(request):
        sent.append((request.url.host, request.headers.get(HEADER)))
        if json.loads(request.content).get("stream"):
            chunk = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                                  "finish_reason": "stop"}]}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return _chat_response(request)

    def http_client(*_args, **_kwargs):
        return httpx.Client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(AIAgent, "_build_keepalive_http_client", staticmethod(http_client))
    with frozen_runtime_config(_config()):
        agent = AIAgent(provider="custom", requested_provider=NAMED, base_url=PROXY, api_key="test",
                         model=MODEL, api_mode="chat_completions", enabled_toolsets=[], quiet_mode=True,
                         skip_context_files=True, skip_memory=True, skip_background_review=True)
        monkeypatch.setattr(agent, "_disable_streaming", True, raising=False)
        try:
            first = agent.run_conversation("Hello", turn_origin="user")
            assert first["final_response"] == "ok"
            second = agent.run_conversation("Wake", first["messages"], turn_origin="goal_loop")
            assert second["final_response"] == "ok"
            agent.switch_model(MODEL, "custom", api_mode="chat_completions",
                               base_url="https://direct.example.test/v1", api_key="test")
            assert agent.run_conversation("Hello again", turn_origin="user")["final_response"] == "ok"
        finally:
            agent.close()
    assert sent == [("proxy.example.test", "interactive"), ("proxy.example.test", "background"),
                    ("direct.example.test", None)]


@pytest.mark.parametrize("intervention", ["steer", "redirect"])
def test_human_intervention_promotes_goal_loop_on_wire(monkeypatch, intervention):
    from run_agent import AIAgent

    primary, auxiliary = [], []
    correction = "Use the narrower scope"

    def handle_aux(request):
        auxiliary.append(request.headers.get(HEADER))
        return _chat_response(request)

    def call_aux():
        aux.call_llm(task="compression", provider=NAMED, model=MODEL, messages=MESSAGES)

    def handle_primary(request):
        primary.append((request.headers.get(HEADER), request.content))
        call_aux()
        if len(primary) == 1:
            assert getattr(agent, intervention)(correction)
            if intervention == "steer":
                return httpx.Response(200, json={"id": "tool", "object": "chat.completion", "created": 1,
                    "model": MODEL, "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                        "role": "assistant", "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "probe", "arguments": "{}"}}]}}]})
        return _chat_response(request)

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *_a, **_kw: {
        "http_client": httpx.Client(transport=httpx.MockTransport(handle_aux)),
    })
    monkeypatch.setattr(AIAgent, "_build_keepalive_http_client", staticmethod(
        lambda *_a, **_kw: httpx.Client(transport=httpx.MockTransport(handle_primary)),
    ))
    with frozen_runtime_config(_config()):
        agent = AIAgent(provider="custom", requested_provider=NAMED, base_url=PROXY, api_key="test",
                         model=MODEL, api_mode="chat_completions", enabled_toolsets=[], quiet_mode=True,
                         skip_context_files=True, skip_memory=True, skip_background_review=True)
        monkeypatch.setattr(agent, "_disable_streaming", True, raising=False)
        try:
            first = agent.run_conversation("Wake", turn_origin="goal_loop")
            assert first["final_response"] == "ok"
            assert first["user_intervened"] is True
            call_aux()  # Outside the turn, compression gets its standalone default again.
            assert agent.run_conversation("Wake again", turn_origin="goal_loop")["final_response"] == "ok"
        finally:
            agent.close()
    assert [request_class for request_class, _ in primary] == ["background", "interactive", "background"]
    assert correction.encode() in primary[1][1]
    assert auxiliary == ["background", "interactive", "background", "background"]


@pytest.mark.parametrize(("mode", "endpoint"), [("chat_completions", "chat/completions"),
                                                ("codex_responses", "responses")])
def test_primary_turns_on_wire(mode, endpoint):
    sent = []

    def handle(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(500, json={"error": {"message": "retry"}}, headers={"retry-after-ms": "1"})
        return _chat_response(request)

    agent = SimpleNamespace(api_mode=mode, provider="custom", requested_provider=NAMED, base_url=PROXY,
                            _turn_provenance="user", _is_user_initiated_turn=False)
    kwargs = {"model": MODEL, "extra_headers": {"X-Existing": "kept"}}
    kwargs.update({"input": MESSAGES} if mode == "codex_responses" else {"messages": MESSAGES})
    with frozen_runtime_config(_config(mode=mode)), OpenAI(
        api_key="test", base_url=PROXY, max_retries=1, http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        create = client.responses.create if mode == "codex_responses" else client.chat.completions.create
        create(**apply_turn_request_class(agent, kwargs))
        grown = [*MESSAGES, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "Again"}]
        create(**apply_turn_request_class(agent, {**kwargs, ("input" if mode == "codex_responses" else "messages"): grown}))
        agent._turn_provenance = "cron"
        create(**apply_turn_request_class(agent, kwargs))

    assert [req.headers[HEADER] for req in sent] == ["interactive", "interactive", "interactive", "background"]
    assert [req.url.path for req in sent] == [f"/v1/{endpoint}"] * 4
    assert all(req.headers["X-Existing"] == "kept" for req in sent)
    bodies = [json.loads(req.content) for req in sent]
    assert bodies[0] == bodies[1] == bodies[3]
    key = "input" if mode == "codex_responses" else "messages"
    assert bodies[2][key][:len(MESSAGES)] == bodies[0][key] == MESSAGES
    assert kwargs["extra_headers"] == {"X-Existing": "kept"}


@pytest.mark.parametrize("overrides", [
    {"_turn_provenance": "relay"}, {"_turn_provenance": "goal_loop"},
    {"_turn_provenance": "process"}, {"_turn_origin": "background_review"}, {"_delegate_depth": 1},
    {"is_subagent": True},
])
def test_unattended_turns(overrides, monkeypatch):
    monkeypatch.setenv("HERMES_TURN_ORIGIN", "user")
    agent = SimpleNamespace(**{"api_mode": "chat_completions", "provider": NAMED, "base_url": PROXY,
                               "_turn_provenance": "user", **overrides})
    with frozen_runtime_config(_config()):
        assert apply_turn_request_class(agent, {})["extra_headers"] == {HEADER: "background"}


@pytest.mark.parametrize("origin", [None, "unknown", "future_origin"])
def test_unrecognized_primary_provenance_is_interactive(origin):
    agent = SimpleNamespace(api_mode="chat_completions", provider=NAMED, base_url=PROXY)
    if origin is not None:
        agent._turn_provenance = origin
    with frozen_runtime_config(_config()):
        assert apply_turn_request_class(agent, {})["extra_headers"] == {HEADER: "interactive"}


@pytest.mark.parametrize(("provider", "base_url", "mode", "enabled"), [
    ("openai", "https://api.openai.com/v1", "chat_completions", True),
    ("custom:other", PROXY, "chat_completions", True),
    (NAMED, "https://api.openai.com/v1", "chat_completions", True),
    (NAMED, PROXY + "/other", "chat_completions", True),
    ("custom", PROXY, "chat_completions", True),
    (NAMED, PROXY, "chat_completions", "true"),
    (NAMED, PROXY, "bedrock_converse", True),
])
def test_no_implicit_opt_in(provider, base_url, mode, enabled):
    agent = SimpleNamespace(api_mode=mode, provider=provider, base_url=base_url, _turn_provenance="user")
    with frozen_runtime_config(_config(opt_in=enabled)):
        kwargs = {"extra_headers": {"X-Existing": "kept"}}
        assert apply_turn_request_class(agent, kwargs) is kwargs


@pytest.mark.parametrize("async_mode", [False, True])
def test_named_auxiliary_resolution_and_redirect_on_wire(monkeypatch, async_mode):
    sent = []

    def handle(request):
        sent.append(request)
        if request.url.host == "proxy.example.test":
            return httpx.Response(307, headers={"location": "https://elsewhere.example.test/v1/chat/completions"})
        return _chat_response(request)

    transport = httpx.MockTransport(handle)

    def http_kwargs(_base, *, async_mode=False, **_kwargs):
        cls = httpx.AsyncClient if async_mode else httpx.Client
        return {"http_client": cls(transport=transport, follow_redirects=True)}

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", http_kwargs)
    with frozen_runtime_config(_config()):
        kwargs = {"provider": NAMED, "model": MODEL, "messages": MESSAGES}
        if async_mode:
            result = asyncio.run(aux.async_call_llm(**kwargs))
        else:
            result = aux.call_llm(**kwargs)
    assert result.choices[0].message.content == "ok"
    assert [(req.url.host, req.headers.get(HEADER)) for req in sent] == [
        ("proxy.example.test", "interactive"), ("elsewhere.example.test", None),
    ]
    assert sent[0].headers["X-Proxy-Option"] == "kept"


@pytest.mark.parametrize("provider", ["main", "auto"])
def test_auxiliary_main_inherits_named_route(monkeypatch, provider):
    sent = []

    def handle(request):
        sent.append(request)
        return _chat_response(request)

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *_a, **_kw: {
        "http_client": httpx.Client(transport=httpx.MockTransport(handle)),
    })
    cfg = {**_config(), "model": {"provider": NAMED, "default": MODEL}}
    main = {"provider": "custom", "requested_provider": NAMED, "base_url": PROXY,
            "model": MODEL, "api_key": "test", "api_mode": "chat_completions"}
    with frozen_runtime_config(cfg):
        aux.call_llm(task="compression", provider=provider, main_runtime=main, messages=MESSAGES)
        aux.call_llm(task="compression", provider=provider, messages=MESSAGES,
                     main_runtime={**main, "requested_provider": "custom:other"})
    assert [req.headers.get(HEADER) for req in sent] == ["background", None]


@pytest.mark.parametrize("origin, expected", [("user", "interactive"), ("unknown", "interactive"),
                                              ("cron", "background")])
def test_moa_reference_and_aggregator_follow_parent_on_wire(monkeypatch, origin, expected):
    from agent import moa_loop

    sent = []

    def handle(request):
        sent.append(request.headers.get(HEADER))
        return _chat_response(request)

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", lambda *_a, **_kw: {
        "http_client": httpx.Client(transport=httpx.MockTransport(handle)),
    })
    parent = SimpleNamespace(_turn_provenance=origin)
    with frozen_runtime_config(_config()):
        for task in ("moa_reference", "moa_aggregator"):
            moa_loop._call_slot(parent, task=task, provider=NAMED, model=MODEL, messages=MESSAGES)
    assert sent == [expected, expected]


def test_anthropic_messages_interactive_on_wire():
    from anthropic import Anthropic

    sent = []

    def handle(request):
        sent.append((request.url.path, request.headers.get(HEADER)))
        return httpx.Response(200, json={"id": "msg-test", "type": "message", "role": "assistant",
                                         "content": [], "model": MODEL, "stop_reason": "end_turn",
                                         "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    agent = SimpleNamespace(api_mode="anthropic_messages", provider=NAMED, base_url=PROXY,
                            _turn_provenance="user")
    with frozen_runtime_config(_config(mode="anthropic_messages")), Anthropic(
        api_key="test", base_url=PROXY.removesuffix("/v1"), http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        client.messages.create(**apply_turn_request_class(agent, {"model": MODEL, "messages": MESSAGES, "max_tokens": 32}))
    assert sent == [("/v1/messages", "interactive")]


def test_unexpected_lookup_error_omits_hint(monkeypatch):
    from hermes_cli import runtime_provider_custom

    def broken_lookup(*args, **kwargs):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(runtime_provider_custom, "_get_named_custom_provider", broken_lookup)
    agent = SimpleNamespace(api_mode="chat_completions", provider=NAMED, base_url=PROXY)
    kwargs = {"extra_headers": {"X-Existing": "kept"}}
    assert apply_turn_request_class(agent, kwargs) is kwargs
    monkeypatch.setattr(aux, "_effective_provider_for_client", broken_lookup)
    assert aux._request_class_provider(object(), NAMED) == ""


def test_redirect_guard_idempotent():
    with OpenAI(api_key="test") as client:
        protect_request_class_redirects(client)
        protect_request_class_redirects(client)
        assert len(client._client.event_hooks["request"]) == 1
