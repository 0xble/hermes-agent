"""Incognito through the gateway: a configured private route, a compacting turn, /new and off.

Only the provider boundary is replaced. Model routing, the agent loop, in-place compaction,
transcript persistence, reset and restoration run through their production owners.
"""
import asyncio
import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.incognito import event_scope
from tests.agent.test_run_agent import _mock_response
from tests.gateway.test_incognito_commands import harness as harness

PROVIDERS = {
    "main": {"base_url": "http://127.0.0.1:9/v1", "api_mode": "chat_completions", "api_key": "main-key",
             "models": {"normal-model": {"context_length": 128000}}},
    "private": {"base_url": "http://127.0.0.1:9/v2", "api_mode": "chat_completions", "api_key": "private-key",
                "models": {"private-model": {"context_length": 128000}}},
}


@pytest.fixture
def private_route(harness, monkeypatch, tmp_path):
    """The live shape: a bare incognito.model that a model alias routes to another provider."""
    runner, store, entry, config, event, db = harness
    config.clear()
    config.update({
        "model": {"default": "normal-model", "provider": "custom:main", "context_length": 128000},
        "providers": PROVIDERS,
        "model_aliases": {"private": {"model": "private-model", "provider": "custom:private"}},
        "incognito": {"model": "private-model"},
        "compression": {"enabled": True, "in_place": True, "protect_first_n": 0, "protect_last_n": 2},
        "platform_toolsets": {"telegram": []},
        "checkpoints": {"enabled": False},
    })
    import hermes_yaml
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    # The provider's model listing is the only network read in model routing.
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *a, **kw: ["normal-model", "private-model"],
                        raising=False)
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **kw: {})
    return runner, store, entry, config, event, db


async def incognito(runner, event, text):
    incoming = event(text)
    with event_scope(runner, incoming):
        return await runner._handle_incognito_command(incoming)


def disk_bytes(root):
    return b"".join(path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file())


def test_switch_model_treats_missing_explicit_provider_as_none_given(private_route):
    """Callers without a --provider flag pass None; routing must not depend on the sentinel."""
    from hermes_cli.model_switch import switch_model
    kwargs = dict(raw_input="private-model", current_provider="custom:main", current_model="normal-model",
                  current_base_url=PROVIDERS["main"]["base_url"], current_api_key="main-key",
                  user_providers=PROVIDERS, custom_providers=[])
    unset, empty = switch_model(**kwargs, explicit_provider=None), switch_model(**kwargs, explicit_provider="")
    assert (unset.success, unset.target_provider, unset.new_model) == (
        empty.success, empty.target_provider, empty.new_model) == (True, "custom:private", "private-model")


@pytest.mark.asyncio
async def test_configured_private_model_resolves_its_aliased_route(private_route):
    runner, _store, entry, _config, event, _db = private_route
    answer = await incognito(runner, event, "/incognito on")
    assert "Could not resolve" not in answer, answer
    assert "ON" in answer and "private-model" in answer, answer
    assert entry.model_override["model"] == "private-model"
    assert entry.model_override["provider"] == "custom:private"
    assert "OFF" in await incognito(runner, event, "/incognito off")
    assert entry.model_override is None


@pytest.mark.asyncio
async def test_new_in_private_topic_with_configured_route_releases_the_parked_agent(private_route, monkeypatch):
    """/new never restores the pre-entry agent it parked, so it must retire it, not crash."""
    runner, store, entry, _config, event, db = private_route
    runner._session_db = db
    released = []
    public_agent = SimpleNamespace(model="normal-model", set_incognito=lambda enabled: None,
                                   flush_public_memory=lambda: None)
    runner._agent_cache[entry.session_key] = (public_agent, "public-signature", 0, entry.session_id)
    monkeypatch.setattr(runner, "_release_evicted_agent_soft", lambda agent, **kw: released.append(agent))
    assert "private-model" in await incognito(runner, event, "/incognito on")
    assert runner._incognito_parked_agents[entry.session_key][0] is public_agent
    for name in ("_hm_stop_owner_turn", "_cleanup_old_agent_for_reset", "_clear_conversation_scope_locked_async",
                 "_fire_session_reset_hooks"):
        monkeypatch.setattr(runner, name, AsyncMock())
    monkeypatch.setattr(runner, "_reset_notice_session_info", lambda source: "")
    monkeypatch.setattr(runner, "_is_telegram_topic_lane", lambda source: False)
    monkeypatch.setattr("gateway.session_automation.move_route_wakes", AsyncMock())
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *a, **kw: None)
    new = event("/new")
    async with event_scope(runner, new):
        await runner._handle_reset_command(new)
    for _ in range(50):
        if released:
            break
        await asyncio.sleep(0.01)
    assert released == [public_agent]
    assert entry.session_key not in runner._incognito_parked_agents
    fresh = store.lookup_by_session_key(entry.session_key)
    assert fresh.session_id != entry.session_id and not fresh.incognito


@pytest.mark.asyncio
async def test_private_turn_compacts_replies_and_off_restores_route_and_prefix(private_route, monkeypatch, tmp_path):
    """/incognito on with a configured route, a turn that needs compaction, the reply, then off."""
    from agent.incognito import is_incognito
    from gateway.session import SessionEntry
    from run_agent import AIAgent
    runner, store, entry, _config, event, db = private_route
    runner._session_db = db
    sid, key = entry.session_id, entry.session_key
    for turn in range(30):
        store.append_to_transcript(sid, {"role": "user", "content": f"Saved question {turn}: " + "detail " * 120})
        store.append_to_transcript(sid, {"role": "assistant", "content": f"Saved answer {turn}: " + "answer " * 120})
    saved, saved_rows = store.load_transcript(sid), db.get_messages(sid)
    assert "private-model" in await incognito(runner, event, "/incognito on")

    canary = "canary-" + secrets.token_hex(12)
    summary = "Compacted earlier conversation for the private turn."
    model_calls, summary_calls, agents = [], [], []

    def summarize(**kwargs):
        summary_calls.append(is_incognito())
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=summary),
                                                        finish_reason="stop")])

    monkeypatch.setattr("agent.context_compressor.call_llm", summarize)

    async def run_agent(*, message, history, session_id, **_kw):
        # The production resolver builds the private agent from the session's override.
        override = runner._session_model_override(key)
        agent = AIAgent(model=override["model"], provider=override["provider"], base_url=override["base_url"],
                        api_key=override["api_key"] or "private-key", api_mode="chat_completions",
                        session_db=db, session_id=session_id, incognito=True, platform="telegram",
                        enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True)
        agents.append(agent)
        # A long session's cached agent carries the provider's last real prompt reading,
        # which anchors preflight compaction on this turn.
        agent.context_compressor.threshold_tokens = 2000
        agent.context_compressor.last_real_prompt_tokens = 40000

        def respond(api_kwargs):
            model_calls.append((api_kwargs.get("model"), json.dumps(api_kwargs.get("messages"))))
            return _mock_response(content="OK")

        agent._interruptible_api_call = respond
        agent._interruptible_streaming_api_call = lambda api_kwargs, **kw: respond(api_kwargs)
        result = await asyncio.to_thread(agent.run_conversation, message, conversation_history=history)
        return {**result, "session_id": session_id}

    runner._run_agent = run_agent
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    source = event("").source
    runner._session_state(key).persistent.run_generation = 1
    turn = event(f"{canary}: reply with just OK")
    turn.message_id = "private-1"
    with event_scope(runner, turn):
        reply = await runner._handle_message_with_agent(turn, source, key, 1)

    assert model_calls and model_calls[-1][0] == "private-model", (len(agents), reply)
    assert reply == "OK", reply
    assert summary_calls and all(summary_calls)
    assert canary in model_calls[-1][1]
    with event_scope(runner, event("/incognito status")):
        private = store.load_transcript(sid)
    assert summary in json.dumps(private) and canary in json.dumps(private)
    assert len(private) < len(saved)
    assert db.get_messages(sid) == saved_rows
    assert canary.encode() not in disk_bytes(tmp_path)

    assert "OFF" in await incognito(runner, event, "/incognito off")
    assert entry.model_override is None and runner._session_model_override(key) is None
    assert store.load_transcript(sid) == saved
    assert db.get_messages(sid) == saved_rows
    assert canary.encode() not in disk_bytes(tmp_path)
    assert isinstance(store.lookup_by_session_key(key), SessionEntry)
    assert not is_incognito(sid)


@pytest.mark.asyncio
async def test_failed_private_turn_records_only_a_content_free_failure_class(private_route, caplog):
    """The private turn's own records are redacted whole, so the failure path names the class."""
    import logging
    runner, _store, entry, _config, event, db = private_route
    runner._session_db = db
    key = entry.session_key
    assert "private-model" in await incognito(runner, event, "/incognito on")
    canary = "canary-" + secrets.token_hex(12)

    async def run_agent(**_kw):
        raise ValueError(f"provider echoed {canary}")

    runner._run_agent = run_agent
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_state(key).persistent.run_generation = 1
    turn = event(f"{canary}: reply with just OK")
    turn.message_id = "private-2"
    caplog.set_level(logging.INFO)
    with event_scope(runner, turn):
        reply = await runner._handle_message_with_agent(turn, event("").source, key, 1)

    assert "couldn't finish this reply" in reply
    records = [record.getMessage() for record in caplog.records]
    assert not any(canary in text for text in records)
    failures = [text for text in records if text.startswith("Incognito agent turn failed:")]
    assert failures == [f"Incognito agent turn failed: builtins.ValueError at {__name__}."
                        f"{run_agent.__code__.co_qualname}:{run_agent.__code__.co_firstlineno + 1}"], records
