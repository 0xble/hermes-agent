"""Auxiliary calls made for a private session use its configured incognito route.

Compression summaries, titles, vision, smart approvals and every other auxiliary task that can
see turn content would otherwise send private content to the task's own provider. Only the
OpenAI-compatible client is replaced: route resolution and credentials run for real.
"""
import asyncio
import json
from types import SimpleNamespace

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
                raise RuntimeError("private route unavailable")
            return _mock_response(content="summary")

        def create(**request):
            return respond(**request)

        async def acreate(**request):
            return respond(**request)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                                   create=acreate if self.asynchronous else create)),
                               api_key=api_key, base_url=base_url, close=lambda: None)


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
    configure(config, tmp_path)
    config["auxiliary"]["compression"]["fallback_chain"] = [{"provider": "custom:main", "model": "normal-model"}]
    configure(config, tmp_path)
    recorder = _Clients(fail_private=True)
    monkeypatch.setattr("agent.auxiliary_client.OpenAI", recorder)
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
        targeted.append(real_target(agent, cfg))
        return None  # stop before binding; which route would bind is what is observed

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


@pytest.mark.parametrize("model_cfg", [
    {"default": "normal-model", "provider": "custom:main", "context_length": 128000},
    {"default": "normal-model", "provider": "custom", "base_url": MAIN_URL, "api_key": "main-key"},
], ids=["named-main", "bare-custom-main"])
def test_bare_custom_private_route_keeps_its_own_endpoint(private_route, tmp_path, clients, model_cfg):
    """A route resolved to a direct endpoint is provider ``custom``: its base_url is the route."""
    import hermes_yaml
    from agent.auxiliary_client import call_llm
    from agent.incognito import incognito_scope, register_session, set_private_route
    _runner, _store, _entry, config, _event, _db = private_route
    configure(config, tmp_path)
    config["model"] = model_cfg
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    sid = "bare-custom-private"
    register_session(sid, True)
    try:
        set_private_route([sid], {"provider": "custom", "model": "private-model", "base_url": PRIVATE_URL})
        with incognito_scope(True, sid):
            call_llm(task="compression", messages=[{"role": "user", "content": "private"}], timeout=5)
    finally:
        register_session(sid, False)
    assert clients.calls and {url for url, _key in clients.calls} == {PRIVATE_URL}


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
