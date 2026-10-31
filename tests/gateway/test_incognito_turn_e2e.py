"""Incognito through the gateway: a configured private route, a compacting turn, /new and off.

Model routing, the agent loop, in-place compaction, transcript persistence, reset and
restoration run through their production owners. The resolver test replaces only the
provider client, Hindsight's cloud client and the summary call. The failure-record tests
replace ``_run_agent`` to inject an exception, and the /new test observes the parked
agent's release instead of tearing down a real one.
"""
import asyncio
import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.incognito import event_scope
from tests.agent.test_run_agent import _mock_response
from tests.gateway import test_incognito_commands

harness = test_incognito_commands.harness  # the shared incognito fixture

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


def started(runner, db):
    """Startup state GatewayRunner.__init__ would set; the harness builds the runner bare."""
    from gateway.config import Platform
    from tests.gateway.test_stale_finalize_suppression import FinalizeCaptureAdapter
    adapter = FinalizeCaptureAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._session_db = db
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._prefill_messages, runner._ephemeral_system_prompt = [], ""
    runner._reasoning_config, runner._provider_routing, runner._fallback_model = None, {}, None
    return adapter


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
    started(runner, db)
    released = []
    public_agent = SimpleNamespace(model="normal-model", set_incognito=lambda enabled: None,
                                   flush_public_memory=lambda: None)
    runner._agent_cache[entry.session_key] = (public_agent, "public-signature", 0, entry.session_id)
    monkeypatch.setattr(runner, "_release_evicted_agent_soft", lambda agent, **kw: released.append(agent))
    assert "private-model" in await incognito(runner, event, "/incognito on")
    assert runner._incognito_parked_agents[entry.session_key][0] is public_agent
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


class _ProviderBoundary:
    """The only replaced seam: the OpenAI-compatible client every resolver-built agent uses."""

    def __init__(self):
        self.calls, self.replies = [], []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def __call__(self, *args, **kwargs):
        return self

    def create(self, **kwargs):
        self.calls.append((kwargs.get("model"), json.dumps(kwargs.get("messages"))))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return _mock_response(content=reply, usage={
            "prompt_tokens": 40000, "completion_tokens": 3, "total_tokens": 40003})

    def close(self):
        return None


@pytest.mark.asyncio
async def test_resolver_built_private_turns_compact_reuse_the_agent_and_classify_failure(
        private_route, monkeypatch, tmp_path, caplog, capsys):
    """Real gateway turns: production agent resolver and cache, only the provider client replaced."""
    import logging
    from agent.incognito import is_incognito
    runner, store, entry, config, event, db = private_route
    config["memory"] = {"provider": "hindsight"}
    # Real usage from the first reply (40,000 prompt tokens) anchors compaction on the next turn.
    config["compression"]["threshold_tokens"] = 20000
    config["streaming"] = {"enabled": False}
    config["display"] = {"tool_progress": "off", "interim_assistant_messages": False}
    import hermes_yaml
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    (tmp_path / "hindsight").mkdir(exist_ok=True)
    (tmp_path / "hindsight" / "config.json").write_text(json.dumps({
        "mode": "cloud", "api_url": "https://hindsight.invalid", "bank_id": "private-e2e",
        "auto_retain": True, "retain_every_n_turns": 1, "retain_async": False}))
    hindsight = SimpleNamespace(
        aretain=AsyncMock(return_value=SimpleNamespace()), aretain_batch=AsyncMock(return_value=SimpleNamespace()),
        arecall=AsyncMock(return_value=SimpleNamespace(results=[])), aclose=AsyncMock(), close=lambda: None)
    from plugins.memory.hindsight import HindsightMemoryProvider
    monkeypatch.setattr(HindsightMemoryProvider, "_new_cloud_client", lambda self: hindsight)
    provider = _ProviderBoundary()
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", provider)
    summary = "Compacted earlier conversation for the private turn."
    summaries = []

    def summarize(**kwargs):
        summaries.append(is_incognito())
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=summary),
                                                        finish_reason="stop")])

    monkeypatch.setattr("agent.context_compressor.call_llm", summarize)
    adapter = started(runner, db)
    sid, key = entry.session_id, entry.session_key
    for turn in range(30):
        store.append_to_transcript(sid, {"role": "user", "content": f"Saved question {turn}: " + "detail " * 120})
        store.append_to_transcript(sid, {"role": "assistant", "content": f"Saved answer {turn}: " + "answer " * 120})
    saved, saved_rows = store.load_transcript(sid), db.get_messages(sid)
    assert "private-model" in await incognito(runner, event, "/incognito on")
    canary = "canary-" + secrets.token_hex(12)
    source = event("").source

    async def turn(text, message_id):
        runner._session_state(key).persistent.run_generation += 1
        incoming = event(text)
        incoming.message_id = message_id
        with event_scope(runner, incoming):
            return await runner._handle_message_with_agent(
                incoming, source, key, runner._session_state(key).persistent.run_generation)

    caplog.set_level(logging.INFO)
    provider.replies = ["OK"]
    first = await turn(f"{canary}: reply with just OK", "private-1")
    agent = runner._cached_agent_for(key)
    provider.replies = ["Second OK"]
    second = await turn("Private follow-up", "private-2")
    # A 429 runs the real private path through the credential pool, whose status is persisted.
    limited = RuntimeError(f"rate limited while reading {canary}")
    limited.status_code = 429
    provider.replies = [limited] * 12
    failed = await turn("Private failure", "private-3")

    assert (first, second) == ("OK", "Second OK"), adapter.sent
    assert failed and failed not in {"OK", "Second OK"}
    assert {model for model, _ in provider.calls} == {"private-model"}
    assert canary in provider.calls[0][1]
    assert summaries and all(summaries)
    # The cache re-baseline keeps the resolver's agent across private turns.
    assert agent is not None and agent.incognito is True and runner._cached_agent_for(key) is agent
    assert store.lookup_by_session_key(key).last_prompt_tokens > 0
    records = [record.getMessage() for record in caplog.records]
    assert not any(canary in text for text in records)
    assert any(text.startswith("Incognito agent turn failed:") for text in records), records
    with event_scope(runner, event("/incognito status")):
        private = json.dumps(store.load_transcript(sid))
    assert summary in private and canary in private and "Second OK" in private
    hindsight.aretain.assert_not_called()
    hindsight.aretain_batch.assert_not_called()
    assert db.get_messages(sid) == saved_rows
    assert canary.encode() not in disk_bytes(tmp_path)
    # A gateway's stdout is a log file, and it bypasses the privacy log filter.
    printed = capsys.readouterr()
    assert canary not in printed.out and canary not in printed.err

    assert "OFF" in await incognito(runner, event, "/incognito off")
    assert entry.model_override is None and runner._session_model_override(key) is None
    assert store.load_transcript(sid) == saved and db.get_messages(sid) == saved_rows
    assert runner._cached_agent_for(key) is None
    assert canary.encode() not in disk_bytes(tmp_path)
    assert not is_incognito(sid)


def test_failure_class_keeps_only_source_code_names():
    """A dynamically built type or code object can carry arbitrary text in its names."""
    from agent.incognito import failure_class
    leaky = type("private words here", (Exception,), {"__module__": "private module words"})
    try:
        raise leaky("private message")
    except leaky as exc:
        text = failure_class(exc)
    assert text.startswith("?.? at ") and "private" not in text, text


@pytest.mark.asyncio
async def test_failure_diagnosis_never_replaces_the_reply(private_route, monkeypatch):
    runner, _store, entry, _config, event, db = private_route
    started(runner, db)
    key = entry.session_key
    assert "private-model" in await incognito(runner, event, "/incognito on")

    async def run_agent(**_kw):
        raise ValueError("provider failure")

    def broken(exc):
        raise RuntimeError("diagnosis failed")

    runner._run_agent = run_agent
    monkeypatch.setattr("agent.incognito.failure_class", broken)
    runner._session_state(key).persistent.run_generation = 1
    turn = event("Private question")
    turn.message_id = "private-4"
    with event_scope(runner, turn):
        reply = await runner._handle_message_with_agent(turn, event("").source, key, 1)
    assert "couldn't finish this reply" in reply


@pytest.mark.asyncio
async def test_unresolvable_private_route_records_a_content_free_class(private_route, monkeypatch, caplog):
    import logging
    runner, _store, entry, _config, event, _db = private_route
    canary = "canary-" + secrets.token_hex(12)

    def unavailable(**_kw):
        raise RuntimeError(f"route lookup echoed {canary}")

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", unavailable)
    caplog.set_level(logging.INFO)
    answer = await incognito(runner, event, "/incognito on")
    assert "Could not resolve the configured model route" in answer and entry.incognito
    records = [record.getMessage() for record in caplog.records]
    assert not any(canary in text for text in records)
    assert any(text.startswith("Incognito model route failed: builtins.RuntimeError at ") for text in records), records
