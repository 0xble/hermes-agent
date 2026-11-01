"""Auxiliary calls made for a private session use its configured incognito route.

Compression summaries, titles, vision, smart approvals and every other auxiliary task that can
see turn content would otherwise send private content to the task's own provider. Only the
OpenAI-compatible client is replaced: route resolution and credentials run for real.
"""
import asyncio
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

    def __call__(self, *, api_key=None, base_url=None, **_kwargs):
        def respond():
            self.calls.append((str(base_url).rstrip("/"), api_key))
            if self.fail_private and str(base_url).rstrip("/") == PRIVATE_URL:
                raise RuntimeError("private route unavailable")
            return _mock_response(content="summary")

        def create(**_kw):
            return respond()

        async def acreate(**_kw):
            return respond()

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
