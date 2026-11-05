"""Auxiliary calls made for a private session use its configured incognito route.

Compression summaries, titles, vision, smart approvals and every other auxiliary task that can
see turn content would otherwise send private content to the task's own provider. Only the
OpenAI-compatible client is replaced: route resolution and credentials run for real.
"""
import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.incognito import event_scope
from gateway.config import GatewayConfig
from gateway.session import SessionStore
from tests.agent.test_run_agent import _mock_response
from tests.gateway import test_incognito_turn_e2e as e2e

harness = e2e.harness
private_route = e2e.private_route
PRIVATE_URL = e2e.PROVIDERS["private"]["base_url"]
MAIN_URL = e2e.PROVIDERS["main"]["base_url"]


class _Clients:
    """Records which endpoint each auxiliary request reached."""

    def __init__(self, fail_private=False, asynchronous=False):
        self.calls, self.fail_private, self.asynchronous = [], fail_private, asynchronous
        self.requests = []

    def __call__(self, *, api_key=None, base_url=None, **_kwargs):
        def respond(**request):
            self.requests.append(request)
            self.calls.append((str(base_url).rstrip("/"), api_key))
            if self.fail_private and str(base_url).rstrip("/") == PRIVATE_URL:
                import httpx
                import openai
                raise openai.APIStatusError("Insufficient credits", body=None, response=httpx.Response(
                    402, request=httpx.Request("POST", str(base_url) + "/chat/completions")))
            response = _mock_response(content="summary")
            return iter([response]) if request.get("stream") else response

        def create(**request):
            return respond(**request)

        async def acreate(**request):
            return respond(**request)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                                   create=acreate if self.asynchronous else create)),
                               api_key=api_key, base_url=base_url, close=lambda: None)


def _install_async_boundary(monkeypatch, recorder):
    # Keep SDK type checks valid even if auxiliary_wire is imported during this call.
    import agent.auxiliary_wire

    class AsyncBoundary:
        def __new__(cls, **kwargs):
            return recorder(**kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", AsyncBoundary)


def configure(config, tmp_path, *, incognito_model=True):
    import hermes_yaml
    config["auxiliary"] = {task: {"provider": "custom:main", "model": "normal-model"}
                           for task in ("compression", "vision", "profile_describer")}
    if not incognito_model:
        config.pop("incognito", None)
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))


@pytest.fixture
def clients(monkeypatch):
    recorder = _Clients()
    monkeypatch.setattr("agent.auxiliary_client.OpenAI", recorder)
    return recorder


def aux(task, runner, event, text="private turn content"):
    from agent.auxiliary_client import call_llm
    with event_scope(runner, event(text)):
        return call_llm(task=task, messages=[{"role": "user", "content": text}], timeout=5)


@pytest.mark.asyncio
async def test_private_auxiliary_calls_use_the_incognito_route(private_route, tmp_path, clients):
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path)
    aux("compression", runner, event)
    assert clients.calls[-1] == (MAIN_URL, "main-key")
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    aux("compression", runner, event)
    assert clients.calls[-1] == (PRIVATE_URL, "private-key")
    # A content-free dashboard task keeps its own route even inside a private scope.
    aux("profile_describer", runner, event)
    assert clients.calls[-1] == (MAIN_URL, "main-key")
    assert "OFF" in await e2e.incognito(runner, event, "/incognito off")
    aux("compression", runner, event)
    assert clients.calls[-1] == (MAIN_URL, "main-key")


@pytest.mark.asyncio
async def test_async_private_auxiliary_call_uses_the_incognito_route(private_route, tmp_path, clients, monkeypatch):
    from agent.auxiliary_client import async_call_llm
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path)
    async_clients = _Clients(asynchronous=True)
    monkeypatch.setattr("openai.AsyncOpenAI", async_clients)
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    with event_scope(runner, event("describe this private photo")):
        await async_call_llm(task="vision", messages=[{"role": "user", "content": "private photo"}], timeout=5)
    assert async_clients.calls[-1] == (PRIVATE_URL, "private-key")


@pytest.mark.asyncio
async def test_failing_private_route_never_falls_back_to_another_provider(private_route, tmp_path, monkeypatch):
    runner, _store, _entry, config, event, _db = private_route
    import hermes_yaml
    from hermes_cli.config import load_config_readonly
    configure(config, tmp_path)
    chain = [{"provider": "custom:main", "model": "normal-model"}]
    config["auxiliary"]["compression"].update(provider="custom:private", model="private-model", fallback_chain=chain)
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    assert load_config_readonly()["auxiliary"]["compression"]["fallback_chain"] == chain
    recorder = _Clients(fail_private=True)
    monkeypatch.setattr("agent.auxiliary_client.OpenAI", recorder)
    # The same persisted chain works publicly, but is forbidden for the private call.
    aux("compression", runner, event)
    assert recorder.calls == [(PRIVATE_URL, "private-key"), (MAIN_URL, "main-key")]
    recorder.calls.clear()
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    with pytest.raises(Exception):
        aux("compression", runner, event)
    assert recorder.calls and {url for url, _key in recorder.calls} == {PRIVATE_URL}


@pytest.mark.asyncio
async def test_restart_restores_the_private_auxiliary_route(private_route, tmp_path, clients):
    runner, store, entry, config, event, db = private_route
    configure(config, tmp_path)
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    from agent.incognito import register_session
    for identity in (entry.session_id, entry.session_key):
        register_session(identity, False)   # the old process is gone
    restarted = SessionStore(store.sessions_dir, GatewayConfig())
    restarted._db = db
    runner.session_store = restarted
    assert restarted.lookup_by_session_key(entry.session_key).incognito
    aux("compression", runner, event)
    assert clients.calls[-1] == (PRIVATE_URL, "private-key")


@pytest.mark.asyncio
async def test_without_incognito_model_private_calls_keep_their_route(private_route, tmp_path, clients):
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path, incognito_model=False)
    answer = await e2e.incognito(runner, event, "/incognito on")
    assert "ON" in answer
    aux("compression", runner, event)
    assert clients.calls[-1] == (MAIN_URL, "main-key")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_private_turn_compaction_summary_uses_the_incognito_route(private_route, tmp_path, clients,
                                                                        monkeypatch):
    """A real private gateway turn compacts in RAM, and its summary request stays on the route."""
    runner, store, entry, config, event, db = private_route
    config["compression"]["threshold_tokens"] = 20000
    configure(config, tmp_path)
    e2e.started(runner, db)
    provider = e2e._ProviderBoundary()
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", provider)
    sid, key = entry.session_id, entry.session_key
    for turn in range(30):
        store.append_to_transcript(sid, {"role": "user", "content": f"Saved question {turn}: " + "detail " * 120})
        store.append_to_transcript(sid, {"role": "assistant", "content": f"Saved answer {turn}: " + "answer " * 120})
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")

    async def turn(text, message_id):
        runner._session_state(key).persistent.run_generation += 1
        incoming = event(text)
        incoming.message_id = message_id
        with event_scope(runner, incoming):
            return await runner._handle_message_with_agent(
                incoming, incoming.source, key, runner._session_state(key).persistent.run_generation)

    provider.replies = ["OK", "Second OK"]
    assert await turn("Private question", "private-1") == "OK"
    assert await turn("Private follow-up", "private-2") == "Second OK"
    assert clients.calls, "the private turn never compacted"
    assert {call for call in clients.calls} == {(PRIVATE_URL, "private-key")}


@pytest.mark.asyncio
@pytest.mark.parametrize("task_route", [
    {"base_url": "http://127.0.0.1:9/task", "api_key": "task-key"},
    {"provider": "custom:private", "base_url": "http://127.0.0.1:9/task", "api_key": "task-key"},
], ids=["bare-base-url", "same-provider-endpoint"])
async def test_private_call_ignores_the_task_endpoint(private_route, tmp_path, clients, task_route):
    """A task's own endpoint, bare or under the route's provider name, must not pull a private call off it."""
    import hermes_yaml
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path)
    config["auxiliary"]["compression"] = dict(task_route)
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    aux("compression", runner, event)
    assert clients.calls[-1] == ("http://127.0.0.1:9/task", "task-key")
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    aux("compression", runner, event)
    assert clients.calls[-1] == (PRIVATE_URL, "private-key")


@pytest.mark.asyncio
async def test_private_vision_never_falls_back_to_auto_backends(private_route, tmp_path, clients, monkeypatch):
    """An incognito route that cannot serve vision fails rather than sending the image elsewhere."""
    from agent import auxiliary_client
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path)
    asked = []
    real = auxiliary_client.resolve_vision_provider_client

    def vision_client(provider=None, **kwargs):
        asked.append(provider)
        return (None, None, None) if provider == "custom:private" else real(provider=provider, **kwargs)

    monkeypatch.setattr(auxiliary_client, "resolve_vision_provider_client", vision_client)
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    with pytest.raises(Exception):
        aux("vision", runner, event, "describe this private photo")
    assert asked and "auto" not in asked
    assert all(url == PRIVATE_URL for url, _key in clients.calls)


@pytest.mark.asyncio
async def test_private_voice_turn_stays_on_the_incognito_route(private_route, tmp_path, monkeypatch):
    """auxiliary.voice_chat rebinds the main agent directly, outside call_llm, so it checks too."""
    import hermes_yaml
    from agent import voice_turn_route
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path)
    config["auxiliary"]["voice_chat"] = {"provider": "custom:main", "model": "normal-model"}
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    targeted = []
    real_target = voice_turn_route._route_target

    def route_target(agent, cfg):
        # Stop before binding. Observe only which route would bind.
        targeted.append(real_target(agent, cfg))

    monkeypatch.setattr(voice_turn_route, "_route_target", route_target)

    def voice_agent():
        return SimpleNamespace(_voice_turn_pending=True, provider="custom:private", model="private-model",
                               base_url=PRIVATE_URL, api_key="private-key", api_mode="chat_completions",
                               _cached_system_prompt=None, reasoning_config=None)

    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    with event_scope(runner, event("a private voice note")):
        assert voice_turn_route.begin_voice_turn_route(voice_agent(), [], "system") == "system"
    assert targeted == []
    assert "OFF" in await e2e.incognito(runner, event, "/incognito off")
    voice_turn_route.begin_voice_turn_route(voice_agent(), [], "system")
    assert [target["provider"] for target in targeted] == ["custom:main"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("route", [
    {"provider": "custom", "model": "private-model", "base_url": PRIVATE_URL},
    {"provider": "custom", "model": "private-model"},
], ids=["with-base-url", "without-base-url"])
async def test_bare_custom_private_route_is_refused_and_sends_nothing(
        private_route, tmp_path, clients, monkeypatch, route, asynchronous):
    """A bare endpoint names no provider entry to take its key from, so it is never routed."""
    import hermes_yaml
    from agent.auxiliary_client import call_llm, async_call_llm
    from agent.incognito import incognito_scope, register_session, set_private_route
    _runner, _store, _entry, config, _event, _db = private_route
    configure(config, tmp_path)
    config["model"] = {"default": "normal-model", "provider": "custom", "base_url": MAIN_URL, "api_key": "main-key"}
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    recorder = _Clients(asynchronous=asynchronous)
    monkeypatch.setattr("agent.auxiliary_client.OpenAI", clients if asynchronous else recorder)
    _install_async_boundary(monkeypatch, recorder)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    sid = "bare-custom-private"
    register_session(sid, True)
    kwargs: dict[str, Any] = dict(task="compression", messages=[{"role": "user", "content": "private"}], timeout=5,
                  api_key="task-key", main_runtime={"api_key": "main-key", "api_mode": "codex_responses"})
    refusal = None
    try:
        set_private_route([sid], route)
        with incognito_scope(True, sid):
            try:
                if asynchronous:
                    await async_call_llm(**kwargs)
                else:
                    call_llm(**kwargs, api_mode="codex_responses")
            except RuntimeError as exc:
                refusal = exc
    finally:
        register_session(sid, False)
    assert recorder.calls == [] and clients.calls == []
    assert refusal is not None and "must use a named provider" in str(refusal)
    # Public calls keep their configured route and credential.
    kwargs = dict(task="compression", messages=[{"role": "user", "content": "public"}], timeout=5)
    if asynchronous:
        await async_call_llm(**kwargs)
    else:
        call_llm(**kwargs)
    assert recorder.calls == [(MAIN_URL, "main-key")]


@pytest.mark.asyncio
async def test_task_extra_body_stays_with_the_task_route(private_route, tmp_path, clients):
    """auxiliary.<task>.extra_body and reasoning_effort shape that task's own provider only."""
    import hermes_yaml
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path)
    config["auxiliary"]["compression"].update(extra_body={"task_only_flag": True}, reasoning_effort="high")
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    aux("compression", runner, event)
    assert "task_only_flag" in (clients.requests[-1].get("extra_body") or {})
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    aux("compression", runner, event)
    private_body = clients.requests[-1].get("extra_body") or {}
    assert clients.calls[-1][0] == PRIVATE_URL
    assert "task_only_flag" not in private_body and "reasoning" not in private_body


@pytest.mark.asyncio
async def test_mcp_sampling_during_a_private_tool_call_uses_the_incognito_route(private_route, tmp_path, clients):
    """The MCP recv loop starts with an empty context; sampling replays the tool call's snapshot."""
    import contextvars
    from tools.mcp_tool_sampling import SamplingHandler
    runner, _store, _entry, config, event, _db = private_route
    configure(config, tmp_path)
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    with event_scope(runner, event("call the mcp tool with private content")):
        tool_call_context = contextvars.copy_context()
    handler = SamplingHandler("srv", {}, call_context=lambda: tool_call_context)
    params = SimpleNamespace(
        messages=[SimpleNamespace(role="user", content=SimpleNamespace(type="text", text="private tool argument"))],
        model_preferences=None, modelPreferences=None, system_prompt=None, systemPrompt=None,
        max_tokens=64, maxTokens=64, temperature=None, tools=None)
    recv_loop = asyncio.get_running_loop().create_task(handler(None, params), context=contextvars.Context())
    await recv_loop
    assert clients.calls and clients.calls[-1] == (PRIVATE_URL, "private-key")


class _OverloadedPrivateRoute:
    """Main-turn client: the private endpoint answers 503, any other endpoint would answer."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, api_key=None, base_url=None, **kwargs):
        import httpx
        import openai
        url = str(base_url).rstrip("/")

        def create(**request):
            self.calls.append((url, json.dumps(request.get("messages"))))
            if url == PRIVATE_URL:
                raise openai.InternalServerError(
                    "upstream overloaded", body=None,
                    response=httpx.Response(503, request=httpx.Request("POST", url + "/chat/completions")))
            return _mock_response(content="fallback answered")

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                               api_key=api_key, base_url=base_url, close=lambda: None, _client=None)


@pytest.mark.asyncio
async def test_private_main_turn_never_falls_back_to_another_provider(private_route, tmp_path, monkeypatch, clients):
    """fallback_providers would carry the whole private conversation to another provider."""
    import hermes_yaml
    from gateway.incognito import INCOGNITO_ROUTE_UNAVAILABLE
    runner, _store, entry, config, event, db = private_route
    configure(config, tmp_path)
    config["fallback_providers"] = [{"provider": "custom:main", "model": "normal-model"}]
    config["streaming"] = {"enabled": False}
    config["display"] = {"tool_progress": "off", "interim_assistant_messages": False}
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    monkeypatch.setattr("agent.retry_utils.jittered_backoff", lambda *a, **kw: 0.0)
    e2e.started(runner, db)
    boundary = _OverloadedPrivateRoute()
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", boundary)
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    key = entry.session_key
    runner._session_state(key).persistent.run_generation += 1
    turn = event("PRIVATE-CANARY question")
    turn.message_id = "private-1"
    with event_scope(runner, turn):
        reply = await runner._handle_message_with_agent(
            turn, turn.source, key, runner._session_state(key).persistent.run_generation)
    assert boundary.calls and all(url == PRIVATE_URL for url, _messages in boundary.calls), boundary.calls
    assert reply.startswith(INCOGNITO_ROUTE_UNAVAILABLE) and "fallback" not in reply, reply


class _RecoveryClients:
    """Only the physical provider is scripted; resolution and recovery run unchanged."""

    def __init__(self, status, asynchronous=False):
        self.status, self.asynchronous, self.calls = status, asynchronous, []

    def __call__(self, *, api_key=None, base_url=None, **_kwargs):
        import httpx
        import openai

        def respond(request):
            self.calls.append((str(base_url).rstrip("/"), api_key, request["model"]))
            if len(self.calls) == 1:
                message = {404: "Model not found", 401: "Unauthorized", 402: "Insufficient credits"}[self.status]
                error_type = {404: openai.NotFoundError, 401: openai.AuthenticationError,
                              402: openai.APIStatusError}[self.status]
                raise error_type(message, body=None, response=httpx.Response(
                    self.status, request=httpx.Request("POST", str(base_url) + "/chat/completions")))
            return _mock_response(content="summary")

        def create(**request):
            if request.get("stream"):
                # The first failure arrives only when consumed, outside the caller's private scope.
                def chunks():
                    response = respond(request)
                    from agent.auxiliary_call_policy import completed_response_as_stream_chunk
                    yield completed_response_as_stream_chunk(response)
                return chunks()
            return respond(request)

        async def acreate(**request):
            return respond(request)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=acreate if self.asynchronous else create)), api_key=api_key, base_url=base_url,
            close=lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("status", [404, 401, 402], ids=["missing-model", "auth-refresh", "paid-refresh"])
@pytest.mark.parametrize("private", [True, False], ids=["private", "public"])
@pytest.mark.parametrize("provider", ["custom:private", "custom:nous", "nous"],
                         ids=["named-custom", "named-like-built-in", "nous"])
async def test_recovery_preserves_the_private_model_without_disabling_auth_refresh(
        private_route, tmp_path, monkeypatch, mode, status, private, provider):
    """Private recovery pins the model; public catalog healing and same-route auth still work."""
    from contextlib import nullcontext
    from copy import deepcopy
    import hermes_yaml
    import openai
    from agent.auxiliary_client import call_llm, async_call_llm
    from agent.incognito import incognito_scope, register_session, set_private_route
    from agent.auxiliary_call_policy import private_route_active
    _runner, _store, _entry, config, _event, _db = private_route
    configure(config, tmp_path)
    config["providers"] = deepcopy(config["providers"])
    url = "https://inference-api.nousresearch.com/v1"
    named_url = "https://inference-api.nousresearch.com/private-proxy/v1"
    config["providers"]["private"].update(base_url=named_url, api_key="named-private-key")
    config["providers"]["nous"] = {**config["providers"]["private"], "name": "nous"}

    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    recorder = _RecoveryClients(status, asynchronous=(mode == "async"))
    monkeypatch.setattr("agent.auxiliary_client.OpenAI", recorder if mode != "async" else _Clients())
    _install_async_boundary(monkeypatch, recorder)
    # External catalog/account/auth boundaries return synthetic data, never a live provider call.
    monkeypatch.setattr("hermes_cli.models.get_nous_recommended_aux_model", lambda **kw: "other-recommended-model")
    monkeypatch.setattr("agent.auxiliary_client._nous_portal_account_has_fresh_paid_access", lambda: True)
    monkeypatch.setattr("hermes_cli.auth.resolve_nous_runtime_credentials",
                        lambda **kw: {"api_key": "refreshed-builtin-key" if kw.get("force_refresh") else "builtin-key",
                                      "base_url": url})
    sid = "model-pinned-private"
    register_session(sid, True)
    try:
        set_private_route([sid], {"provider": provider, "model": "private-model"})
        kwargs: dict[str, Any] = dict(task="compression", provider=provider, model="private-model",
                      messages=[{"role": "user", "content": "private summary input"}], timeout=5)
        # Named custom entries cannot borrow the host provider's refreshed credential either.
        named = provider.startswith("custom:")
        must_refuse = (status == 404 and (private or named)) or (named and (private or status == 401))
        error = {404: "Model not found", 401: "Unauthorized", 402: "Insufficient credits"}[status]
        failure = None
        with incognito_scope(True, sid) if private else nullcontext():
            try:
                if mode == "async":
                    await async_call_llm(**kwargs)
                else:
                    result = call_llm(**kwargs, stream=(mode == "stream"))
            except openai.APIStatusError as exc:
                failure = exc
        assert not private_route_active()
        if mode == "stream":
            try:
                assert list(result)
            except openai.APIStatusError as exc:
                failure = exc
            assert not private_route_active()
    finally:
        register_session(sid, False)
    first = (named_url, "named-private-key", "private-model") if provider.startswith("custom:") else (
        url, "builtin-key", "private-model")
    if must_refuse:
        expected = [first]
    elif named:
        # Public payment fallback is still allowed; it is not a credential refresh
        # of the saved entry. Private calls above never reach this route.
        expected = [first, (MAIN_URL, "main-key", "normal-model")]
    elif status == 404:
        expected = [first, (url, "builtin-key", "other-recommended-model")]
    else:
        expected = [first, (url, "refreshed-builtin-key", "private-model")]
    assert recorder.calls == expected
    if must_refuse:
        assert failure is not None and failure.status_code == status and error in str(failure)
    else:
        assert failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("name", ["private", "nous", "openrouter", "codex", "main", "auto", "moa", "kimi", "llamacpp"])
async def test_named_private_responses_route_owns_its_key_model_and_wire_mode(
        private_route, tmp_path, monkeypatch, mode, name):
    """The named entry, not task/main/env settings, supplies the private credential and transport."""
    from copy import deepcopy
    import hermes_yaml
    from agent.auxiliary_client import call_llm, async_call_llm
    from agent.incognito import incognito_scope, register_session, set_private_route
    from tests.agent.test_run_agent_codex_responses import _FakeCreateStream
    _runner, _store, _entry, config, _event, _db = private_route
    configure(config, tmp_path)
    config["providers"] = deepcopy(config["providers"])
    config["providers"][name] = {**config["providers"]["private"], "api_mode": "codex_responses"}
    config["auxiliary"]["compression"].update(api_key="task-key", api_mode="chat_completions")
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "builtin-openrouter-key")
    monkeypatch.setattr("hermes_cli.auth.resolve_nous_runtime_credentials",
                        lambda **kw: {"api_key": "builtin-nous-key", "base_url": "https://builtin.invalid/v1"})
    monkeypatch.setattr("hermes_cli.auth.resolve_codex_runtime_credentials",
                        lambda **kw: {"api_key": "builtin-codex-key", "base_url": "https://builtin.invalid/v1"})
    calls = []

    def boundary(*, api_key=None, base_url=None, _async=False, **kw):
        def responses(**request):
            calls.append((str(base_url).rstrip("/"), api_key, request["model"], "responses"))
            return _FakeCreateStream([
                SimpleNamespace(type="response.output_item.done", item=SimpleNamespace(
                    type="message", status="completed", content=[SimpleNamespace(type="output_text", text="summary")])),
                SimpleNamespace(type="response.completed", response=SimpleNamespace(status="completed", id="r1",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2))),
            ])

        def chat(**request):
            calls.append((str(base_url).rstrip("/"), api_key, request["model"], "chat"))
            response = _mock_response(content="summary")
            return iter([response]) if request.get("stream") else response

        async def async_chat(**request):
            return chat(**request)

        return SimpleNamespace(responses=SimpleNamespace(create=responses), api_key=api_key, base_url=base_url,
                               chat=SimpleNamespace(completions=SimpleNamespace(
                                   create=async_chat if _async else chat)), close=lambda: None)

    monkeypatch.setattr("agent.auxiliary_client.OpenAI", boundary)
    _install_async_boundary(monkeypatch, lambda **kw: boundary(_async=True, **kw))
    sid = "responses-private"
    register_session(sid, True)
    try:
        set_private_route([sid], {"provider": f"custom:{name}", "model": "private-model"})
        result = None
        with incognito_scope(True, sid):
            kwargs: dict[str, Any] = dict(task="compression", messages=[{"role": "user", "content": "private summary input"}],
                          api_key="caller-key", timeout=5,
                          main_runtime={"provider": "custom:main", "model": "normal-model", "api_key": "main-key",
                                        "base_url": MAIN_URL, "api_mode": "chat_completions"})
            if mode == "async":
                await async_call_llm(**kwargs)
            else:
                result = call_llm(**kwargs, api_mode="chat_completions", stream=(mode == "stream"))
        if mode == "stream":
            # The real Responses adapter currently materializes the stream into a completion.
            assert result is not None
            assert result.choices if hasattr(result, "choices") else list(result)
    finally:
        register_session(sid, False)
    assert calls == [(PRIVATE_URL, "private-key", "private-model", "responses")]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("entry", [None, {"enabled": False, "base_url": PRIVATE_URL, "api_key": "private-key"},
                                  {"api_key": "private-key"}], ids=["missing", "disabled", "missing-endpoint"])
async def test_unusable_named_private_route_never_dispatches_a_builtin(
        private_route, tmp_path, monkeypatch, mode, entry):
    """An explicit saved-entry identity is never permission to fall through to its built-in namesake."""
    from copy import deepcopy
    import hermes_yaml
    from agent.auxiliary_client import call_llm, async_call_llm
    from agent.incognito import incognito_scope, register_session, set_private_route
    _runner, _store, _entry, config, _event, _db = private_route
    configure(config, tmp_path)
    config["providers"] = deepcopy(config["providers"])
    if entry is not None:
        config["providers"]["nous"] = entry
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    monkeypatch.setattr("hermes_cli.auth.resolve_nous_runtime_credentials",
                        lambda **kw: {"api_key": "builtin-key", "base_url": "https://builtin.invalid/v1"})
    recorder = _Clients(asynchronous=(mode == "async"))
    monkeypatch.setattr("agent.auxiliary_client.OpenAI", recorder if mode != "async" else _Clients())
    _install_async_boundary(monkeypatch, recorder)
    sid = "missing-named-private"
    register_session(sid, True)
    try:
        set_private_route([sid], {"provider": "custom:nous", "model": "private-model"})
        with pytest.raises(RuntimeError):
            with incognito_scope(True, sid):
                kwargs = dict(task="compression", messages=[{"role": "user", "content": "private summary"}], timeout=5)
                if mode == "async":
                    await async_call_llm(**kwargs)
                else:
                    result = call_llm(**kwargs, stream=(mode == "stream"))
            if mode == "stream":
                list(result)
    finally:
        register_session(sid, False)
    assert recorder.calls == []
