"""Regression tests for restoring a rate-limited primary during a long turn."""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
from openai import InternalServerError, RateLimitError

from agent.error_classifier import FailoverReason
from agent.shared_primary_cooldown import complete_primary_recovery
from run_agent import AIAgent


@pytest.fixture
def recovery_runtime():
    from agent import fallback_route_recovery
    return fallback_route_recovery


def test_restores_primary_prompt_identity_when_runtime_recovers(recovery_runtime):
    restore = Mock(return_value=True)
    agent = SimpleNamespace(
        _fallback_activated=True, _provider_fallback_active=True, _fallback_episode_cooled=True,
        _rate_limited_until=1,
        _cached_system_prompt="Model: primary\nProvider: primary-provider",
        _restore_primary_runtime=restore,
    )

    result = recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: fallback")

    assert result == agent._cached_system_prompt
    restore.assert_called_once_with()


def test_keeps_fallback_prompt_when_cooldown_has_not_expired(recovery_runtime):
    restore = Mock(return_value=False)
    agent = SimpleNamespace(
        _fallback_activated=True, _provider_fallback_active=True, _fallback_episode_cooled=True,
        _rate_limited_until=1,
        _cached_system_prompt="Model: primary",
        _restore_primary_runtime=restore,
    )

    result = recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: fallback")

    assert result == "Model: fallback"
    restore.assert_called_once_with()


def test_skips_restore_probe_for_primary_iterations(recovery_runtime):
    restore = Mock(return_value=True)
    agent = SimpleNamespace(
        _fallback_activated=False,
        _cached_system_prompt="Model: primary",
        _restore_primary_runtime=restore,
    )

    result = recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: primary")

    assert result == "Model: primary"
    restore.assert_not_called()


@pytest.mark.parametrize("recovery", [
    "expired", "cleared", "active", "max_stay", "max_stay_same_entry", "primary_fails_again"
])
def test_long_turn_uses_shared_recovery_and_preserves_tool_pairs(tmp_path, monkeypatch, recovery):
    """Drive the real loop, shared store and route binding through a tool boundary."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tool_defs = [{"type": "function", "function": {
        "name": "web_search", "description": "x", "parameters": {"type": "object", "properties": {}}}}]
    chain = [
        {"provider": "openai", "model": "better-backup", "base_url": "https://better.invalid/v1"},
        {"provider": "openai", "model": "last-resort", "base_url": "https://last.invalid/v1"},
    ]
    if recovery.startswith("max_stay"):
        chain[-1]["max_stay_seconds"] = 1
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="fixture", base_url="https://primary.invalid/v1", provider="custom",
            model="primary-model", api_mode="chat_completions", quiet_mode=True,
            skip_context_files=True, skip_memory=True, fallback_model=chain,
        )
    agent._cached_system_prompt = "Model: primary-model\nProvider: custom"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.valid_tool_names = {"web_search"}
    agent._api_max_retries = 1
    agent._disable_streaming = True
    resolved = []
    tool_finished = []

    def resolve(provider, model=None, **kwargs):
        resolved.append(model)
        if recovery == "max_stay_same_entry" and tool_finished and model == "better-backup":
            return None, model
        client = MagicMock(api_key="fixture", base_url=kwargs.get("base_url", "https://fallback.invalid/v1"))
        return client, model

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve):
        assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
    assert agent.model == "last-resort"
    resolved.clear()
    requested = []

    def request(kwargs):
        requested.append((agent.model, kwargs["messages"]))
        if len(requested) == 1:
            tc = SimpleNamespace(id="paired_call", type="function",
                                 function=SimpleNamespace(name="web_search", arguments="{}"))
            msg = SimpleNamespace(content="", tool_calls=[tc])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")], usage=None)
        assert tool_finished == [True], "never restore before the tool result is returned"
        prior = kwargs["messages"]
        calls = [m for m in prior if m.get("tool_calls")]
        results = [m for m in prior if m.get("tool_call_id") == "paired_call"]
        assert len(calls) == len(results) == 1
        if recovery == "primary_fails_again" and agent.model == "primary-model":
            response = httpx.Response(429, request=httpx.Request("POST", "https://primary.invalid/v1"))
            raise RateLimitError("rate limit", response=response, body={})
        msg = SimpleNamespace(content="finished", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    def tool(*args, **kwargs):
        assert agent.model == "last-resort", "tool execution must retain its issuing route"
        path = tmp_path / "state" / "model_cooldowns.json"
        if recovery == "active":
            # A stale local deadline must not override the shared record.
            agent._rate_limited_until = 0
        if recovery.startswith("max_stay"):
            agent._fallback_stay_started_at = time.monotonic() - 2
        if recovery in ("expired", "primary_fails_again"):
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            for record in state["routes"].values():
                record["reset_at"] = time.time() - 1
            path.write_text(json.dumps(state))
        elif recovery == "cleared":
            # Another session's successful primary response clears the exact shared outage.
            peer = SimpleNamespace(
                _shared_primary_cooldown_record=agent._shared_primary_cooldown_record.copy(),
                _fallback_activated=False, provider="custom", model="primary-model",
                base_url="https://primary.invalid/v1", _emit_diagnostic_status=Mock(),
            )
            assert complete_primary_recovery(peer)
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            assert any(record.get("state") == "tombstone" for record in state["routes"].values())
        tool_finished.append(True)
        return "tool result"

    try:
        with (
            patch.object(agent, "_interruptible_api_call", side_effect=request),
            patch("model_tools.handle_function_call", side_effect=tool),
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("complete the task")
    finally:
        agent.close()

    assert result["final_response"] == "finished"
    models = [model for model, _ in requested]
    expected = {
        "expired": ["last-resort", "primary-model"],
        "cleared": ["last-resort", "primary-model"],
        "active": ["last-resort", "last-resort"],
        "max_stay": ["last-resort", "better-backup"],
        "max_stay_same_entry": ["last-resort", "last-resort"],
        "primary_fails_again": ["last-resort", "primary-model", "better-backup"],
    }
    assert models == expected[recovery]
    if recovery.startswith("max_stay"):
        assert time.monotonic() - agent._fallback_stay_started_at < 1
    if recovery == "primary_fails_again":
        assert resolved == ["better-backup"], "recovery resets the chain to entry zero"
    if recovery in ("expired", "cleared"):
        assert "primary-model" in requested[-1][1][0]["content"]
        assert "last-resort" not in requested[-1][1][0]["content"]


@pytest.mark.parametrize("failure", ["restore", "activate", "exhausted"])
def test_failed_reevaluation_keeps_route_and_resets_timer(failure, recovery_runtime):
    original_client = object()
    compressor = SimpleNamespace(model="last-resort", context_length=1234)
    entry = {"provider": "backup", "model": "last-resort", "max_stay_seconds": 1}
    agent = SimpleNamespace(
        _fallback_activated=True, _fallback_index=2, _fallback_chain=[{}, entry],
        _active_fallback_entry=entry, _fallback_stay_started_at=time.monotonic() - 2,
        _provider_fallback_active=True, _fallback_episode_cooled=True, model="last-resort", provider="backup",
        client=original_client, context_compressor=compressor,
        _cached_system_prompt="Model: last-resort", _rate_limited_until=200,
        _restore_primary_runtime=Mock(return_value=False),
    )

    def fail(*args, **kwargs):
        assert agent._fallback_index == 0
        agent.client = object()
        agent.model = "broken-candidate"
        agent._cached_system_prompt = "Model: broken-candidate"
        compressor.model = "broken-candidate"
        if failure == "exhausted":
            return False
        raise RuntimeError("could not re-evaluate")

    if failure == "restore":
        agent._restore_primary_runtime.side_effect = RuntimeError("shared read failure")
    with patch.object(recovery_runtime, "_try_reclaim_earlier_fallback", side_effect=fail):
        assert recovery_runtime._maybe_recheck_fallback_route_for_iteration(agent, "old prompt") == "old prompt"
    assert agent.client is original_client
    assert agent.model == compressor.model == "last-resort"
    assert agent._cached_system_prompt == "Model: last-resort"
    assert agent._fallback_index == 2
    assert time.monotonic() - agent._fallback_stay_started_at < 1


def test_failed_partial_bind_restores_original_objects_and_retires_only_new_clients(recovery_runtime):
    original_client = Mock()
    original_anthropic = Mock()
    candidate_client = Mock()
    transport_cache = {"route": "last-resort"}
    entry = {"provider": "backup", "model": "last-resort", "max_stay_seconds": 1}
    agent = SimpleNamespace(
        _fallback_activated=True, _fallback_index=2, _fallback_chain=[{}, entry],
        _active_fallback_entry=entry, _fallback_stay_started_at=time.monotonic() - 2,
        _provider_fallback_active=True, _fallback_episode_cooled=True,
        model="last-resort", provider="backup", client=original_client, _anthropic_client=original_anthropic,
        _bedrock_region="us-west-2", _bedrock_guardrail_config={"id": "g"}, _transport_cache=transport_cache,
        _codex_reasoning_replay_enabled=False, _codex_reasoning_replay_rejected=True,
        _cached_system_prompt="Model: last-resort", _restore_primary_runtime=Mock(return_value=False),
    )

    def fail(*args, **kwargs):
        # Mirror bind_route_entry's side effects before a late step raises.
        agent.client = candidate_client
        agent.model = "broken-candidate"
        agent._bedrock_region = "eu-central-1"
        agent._bedrock_guardrail_config = None
        agent._transport_cache.clear()
        agent._codex_reasoning_replay_enabled = True
        agent._codex_reasoning_replay_rejected = False
        agent._active_fallback_entry = {"provider": "other", "model": "broken-candidate"}
        raise RuntimeError("bind failed partway")

    with patch.object(recovery_runtime, "_try_reclaim_earlier_fallback", side_effect=fail):
        assert recovery_runtime._maybe_recheck_fallback_route_for_iteration(agent, "old prompt") == "old prompt"

    assert agent.client is original_client and agent._anthropic_client is original_anthropic
    assert agent.model == "last-resort" and agent._active_fallback_entry is entry
    assert agent._bedrock_region == "us-west-2" and agent._bedrock_guardrail_config == {"id": "g"}
    assert agent._transport_cache == {"route": "last-resort"}
    assert agent._codex_reasoning_replay_enabled is False and agent._codex_reasoning_replay_rejected is True
    assert agent._fallback_index == 2
    original_client.close.assert_not_called()
    original_anthropic.close.assert_not_called()
    candidate_client.close.assert_called_once_with()


@pytest.mark.parametrize("value", [None, 0, -1, False, "bad", float("nan"), float("inf")])
def test_unset_or_invalid_stay_limit_does_not_rewalk_chain(value, recovery_runtime):
    agent = SimpleNamespace(
        _fallback_activated=True, _fallback_index=1,
        _provider_fallback_active=True, _fallback_episode_cooled=True, _rate_limited_until=1,
        _fallback_chain=[{"provider": "backup", "model": "last", "max_stay_seconds": value}],
        _fallback_stay_started_at=time.monotonic() - 100,
        _restore_primary_runtime=Mock(return_value=False),
        _try_activate_fallback=Mock(),
    )
    assert recovery_runtime._maybe_recheck_fallback_route_for_iteration(agent, "old prompt") == "old prompt"
    agent._restore_primary_runtime.assert_called_once()
    agent._try_activate_fallback.assert_not_called()


@pytest.fixture
def tool_loop_agent(tmp_path, monkeypatch):
    """Keep API/tool I/O fake while exercising the real conversation loop."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tool_defs = [{"type": "function", "function": {
        "name": "web_search", "description": "x", "parameters": {"type": "object", "properties": {}}}}]
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="fixture", base_url="https://primary.invalid/v1", provider="custom",
            model="primary-model", api_mode="chat_completions", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
            fallback_model=[{"provider": "openai", "model": "backup", "base_url": "https://backup.invalid/v1"}],
        )
    agent._cached_system_prompt = "Model: primary-model\nProvider: custom"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.valid_tool_names = {"web_search"}
    agent._api_max_retries = 1
    agent._disable_streaming = True
    try:
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=lambda provider, model=None, **kw: (
                MagicMock(api_key="fixture", base_url=kw.get("explicit_base_url") or "https://backup.invalid/v1"), model)),
        ):
            yield agent
    finally:
        agent.close()


def _tool_loop_response(call_id=None):
    calls = None if call_id is None else [SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name="web_search", arguments="{}"))]
    msg = SimpleNamespace(content="finished" if call_id is None else "", tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(
        message=msg, finish_reason="stop" if call_id is None else "tool_calls")], usage=None)


@pytest.mark.parametrize("session_route", ["primary", "fallback"])
def test_voice_turn_keeps_voice_model_across_tool_call(tool_loop_agent, session_route):
    agent = tool_loop_agent
    if session_route == "fallback":
        agent._fallback_chain[0]["max_stay_seconds"] = 1
        assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
    session_model = agent.model
    agent._voice_turn_pending = True
    requested = []

    def request(kwargs):
        requested.append(agent.model)
        if len(requested) == 1:
            return _tool_loop_response("voice_call")
        assert any(m.get("tool_call_id") == "voice_call" for m in kwargs["messages"])
        return _tool_loop_response()

    def tool(*args, **kwargs):
        if session_route == "fallback":
            peer = SimpleNamespace(
                _shared_primary_cooldown_record=agent._shared_primary_cooldown_record.copy(),
                _fallback_activated=False, provider="custom", model="primary-model",
                base_url="https://primary.invalid/v1", _emit_diagnostic_status=Mock(),
            )
            assert complete_primary_recovery(peer)
            agent._fallback_stay_started_at = time.monotonic() - 2
        return "tool result"

    with (
        patch("agent.auxiliary_task_config._get_auxiliary_task_config", return_value={
            "provider": "openai", "model": "voice-model", "base_url": "https://voice.invalid/v1"}),
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", side_effect=tool),
    ):
        result = agent.run_conversation("voice task")

    assert result["final_response"] == "finished"
    assert requested == ["voice-model", "voice-model"]
    assert agent.model == session_model


@pytest.mark.parametrize("voice_fits", [True, False])
def test_voice_binding_preserves_session_fallback_stay(tool_loop_agent, voice_fits):
    from agent.voice_turn_route import begin_voice_turn_route, end_voice_turn_route

    agent = tool_loop_agent
    agent._fallback_chain[0]["max_stay_seconds"] = 1
    assert agent._try_activate_fallback(reason=FailoverReason.server_error)
    entry = agent._active_fallback_entry
    started = agent._fallback_stay_started_at = time.monotonic() - 2
    agent._voice_turn_pending = True
    with (
        patch("agent.auxiliary_task_config._get_auxiliary_task_config", return_value={
            "provider": "openai", "model": "voice-model", "base_url": "https://voice.invalid/v1"}),
        patch("agent.voice_turn_route._fits", return_value=voice_fits),
    ):
        begin_voice_turn_route(agent, [], agent._cached_system_prompt)
        assert agent.model == ("voice-model" if voice_fits else "backup")
        end_voice_turn_route(agent)

    assert agent.model == "backup"
    assert agent._active_fallback_entry is entry
    assert agent._fallback_stay_started_at == started
    assert agent._provider_fallback_active
    assert agent._fallback_index == 1


def test_uncooled_500_primary_is_not_retried_on_tool_iterations(tool_loop_agent, tmp_path):
    agent = tool_loop_agent
    requested = []
    backup_calls = 0

    def request(kwargs):
        nonlocal backup_calls
        requested.append(agent.model)
        if agent.model == "primary-model":
            response = httpx.Response(500, request=httpx.Request("POST", "https://primary.invalid/v1"))
            raise InternalServerError("internal server error", response=response, body={})
        backup_calls += 1
        return _tool_loop_response(f"call_{backup_calls}" if backup_calls < 5 else None)

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", return_value="tool result"),
    ):
        result = agent.run_conversation("complete several tool steps")

    assert result["final_response"] == "finished"
    assert requested == ["primary-model"] + ["backup"] * 5
    assert getattr(agent, "_rate_limited_until", 0) == 0
    assert getattr(agent, "_shared_primary_cooldown_record", None) is None
    assert not (tmp_path / "state" / "model_cooldowns.json").exists()


def test_stale_expired_deadline_does_not_reprobe_uncooled_fallback(tool_loop_agent):
    agent = tool_loop_agent
    agent._rate_limited_until = time.monotonic() - 1000
    requested = []
    backup_calls = 0

    def request(kwargs):
        nonlocal backup_calls
        requested.append(agent.model)
        if agent.model == "primary-model":
            response = httpx.Response(500, request=httpx.Request("POST", "https://primary.invalid/v1"))
            raise InternalServerError("internal server error", response=response, body={})
        backup_calls += 1
        return _tool_loop_response(f"stale_call_{backup_calls}" if backup_calls < 4 else None)

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", return_value="tool result"),
    ):
        result = agent.run_conversation("do not re-probe a stale uncooled fallback")

    assert result["final_response"] == "finished"
    assert requested == ["primary-model"] + ["backup"] * 4


def test_held_recovery_probe_record_does_not_reprobe_uncooled_fallback(tool_loop_agent):
    # After a cooled episode ends, an expired shared record stays cached as a recovery probe.
    # A later uncooled 500 must not treat it as permission to probe the primary mid-turn.
    agent = tool_loop_agent
    requested = []
    backup_calls = 0

    def request(kwargs):
        nonlocal backup_calls
        requested.append(agent.model)
        if agent.model == "primary-model":
            # The record a previous cooled episode left behind is still cached when this
            # uncooled failure starts a new fallback episode.
            agent._shared_primary_cooldown_record = {"reset_at": time.time() - 60, "reason": "billing"}
            response = httpx.Response(500, request=httpx.Request("POST", "https://primary.invalid/v1"))
            raise InternalServerError("internal server error", response=response, body={})
        backup_calls += 1
        return _tool_loop_response(f"probe_call_{backup_calls}" if backup_calls < 4 else None)

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", return_value="tool result"),
        patch("agent.agent_runtime_helpers._refresh_from_shared_cooldown", return_value="expired"),
    ):
        result = agent.run_conversation("do not re-probe on a held recovery record")

    assert result["final_response"] == "finished"
    assert requested == ["primary-model"] + ["backup"] * 4


def test_failed_candidate_bind_does_not_leak_into_next_candidate(recovery_runtime):
    original_client = Mock()
    good_client = Mock()
    broken_client = Mock()
    chain = [
        {"provider": "broken", "model": "first"},
        {"provider": "good", "model": "second"},
        {"provider": "backup", "model": "last-resort"},
    ]
    agent = SimpleNamespace(
        _fallback_chain=chain, _fallback_index=0, _unavailable_fallback_keys=set(),
        model="last-resort", provider="backup", base_url="https://last.invalid",
        client=original_client, _bedrock_region="us-west-2", _transport_cache={"route": "last"},
        _codex_reasoning_replay_rejected=True,
    )

    def bind(agent, entry, provider, model):
        if provider == "broken":
            agent.client = broken_client
            agent.base_url = "https://broken.invalid"
            agent._bedrock_region = "eu-central-1"
            agent._transport_cache.clear()
            agent._codex_reasoning_replay_rejected = False
            raise RuntimeError("bind failed partway")
        previous_route = agent.model, agent.provider
        agent.client = good_client
        agent.model, agent.provider = model, provider
        return previous_route

    with (
        patch("agent.route_binding.bind_route_entry", side_effect=bind),
        patch("agent.chat_completion_helpers._should_skip_fallback_candidate", return_value=False),
        patch("agent.chat_completion_helpers._finalize_fallback_route"),
    ):
        assert recovery_runtime._try_reclaim_earlier_fallback(agent, 2)

    assert agent.client is good_client and agent.model == "second"
    assert agent.base_url == "https://last.invalid"
    assert agent._bedrock_region == "us-west-2"
    assert agent._transport_cache == {"route": "last"}
    assert agent._codex_reasoning_replay_rejected is True
    assert agent._fallback_index == 2
    broken_client.close.assert_called_once_with()
    original_client.close.assert_not_called()


def test_stay_expiry_never_demotes_when_cursor_ran_past_the_active_entry(recovery_runtime):
    # A transient failure on the first backup can walk the cursor to the chain end (later pools
    # cooling) before that same backup is retried successfully. Its stay expiry must reclaim only
    # entries ranked above it, never the later ones the cursor already passed.
    chain = [
        {"provider": "openai", "model": "best-backup", "max_stay_seconds": 1},
        {"provider": "openai", "model": "middle-backup"},
        {"provider": "openai", "model": "worse-backup"},
    ]
    bind = Mock(return_value=True)
    agent = SimpleNamespace(
        _fallback_activated=True, _provider_fallback_active=True, _fallback_episode_cooled=True,
        _fallback_chain=chain, _fallback_index=len(chain), _active_fallback_entry=chain[0],
        _fallback_stay_started_at=time.monotonic() - 2, _unavailable_fallback_keys=set(),
        model="best-backup", provider="openai", client=Mock(),
        _restore_primary_runtime=Mock(return_value=False),
    )
    with (
        patch("agent.route_binding.bind_route_entry", bind),
        patch("agent.chat_completion_helpers._should_skip_fallback_candidate", return_value=False),
        patch("agent.chat_completion_helpers._finalize_fallback_route"),
    ):
        assert recovery_runtime._maybe_recheck_fallback_route_for_iteration(agent, "Model: best") == "Model: best"

    bind.assert_not_called()
    assert agent.model == "best-backup" and agent._active_fallback_entry is chain[0]
    assert agent._fallback_index == len(chain)


def test_uncooled_primary_stay_limit_rewalks_only_fallbacks(tool_loop_agent):
    agent = tool_loop_agent
    agent._api_max_retries = 2  # permits two fallback activations in one logical iteration
    agent._fallback_chain = [
        {"provider": "openai", "model": "better-backup", "base_url": "https://better.invalid/v1"},
        {"provider": "openai", "model": "last-resort", "base_url": "https://last.invalid/v1", "max_stay_seconds": 1},
    ]
    requested = []

    def request(kwargs):
        requested.append(agent.model)
        if agent.model in {"primary-model", "better-backup"} and not any(
            m.get("tool_call_id") == "stay_call" for m in kwargs["messages"]
        ):
            response = httpx.Response(500, request=httpx.Request("POST", agent.base_url))
            raise InternalServerError("internal server error", response=response, body={})
        return _tool_loop_response("stay_call" if agent.model == "last-resort" else None)

    def tool(*args, **kwargs):
        agent._fallback_stay_started_at = time.monotonic() - 2
        return "tool result"

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", side_effect=tool),
    ):
        result = agent.run_conversation("find a better backup")

    assert result["final_response"] == "finished"
    assert requested == ["primary-model"] * 2 + ["better-backup"] * 2 + ["last-resort", "better-backup"]
    assert getattr(agent, "_rate_limited_until", 0) == 0
    assert getattr(agent, "_shared_primary_cooldown_record", None) is None


@pytest.mark.parametrize("start_index,earlier_available,expected_model", [
    (0, False, "best-backup"),
    (1, False, "current-backup"),
    (1, True, "best-backup"),
])
def test_stay_limit_only_reclaims_earlier_backups_quietly(
    tool_loop_agent, start_index, earlier_available, expected_model,
):
    agent = tool_loop_agent
    agent._fallback_chain = [
        {"provider": "openai", "model": "best-backup", "base_url": "https://best.invalid/v1", "max_stay_seconds": 1},
        {"provider": "openai", "model": "current-backup", "base_url": "https://current.invalid/v1", "max_stay_seconds": 1},
        {"provider": "openai", "model": "worse-backup", "base_url": "https://worse.invalid/v1"},
    ]
    agent._fallback_index = start_index
    assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
    original_model = agent.model
    _original_client = agent.client
    original_cooldown = agent._shared_primary_cooldown_record.copy()
    agent._pending_fallback_notice = []
    requested = []
    resolved = []

    def request(kwargs):
        requested.append(agent.model)
        return _tool_loop_response("stay_call" if len(requested) == 1 else None)

    def tool(*args, **kwargs):
        agent._fallback_stay_started_at = time.monotonic() - 2
        return "tool result"

    def resolve(provider, model=None, **kwargs):
        resolved.append(model)
        if model == "best-backup" and not earlier_available:
            return None, model
        return MagicMock(api_key="fixture", base_url=kwargs["explicit_base_url"]), model

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", side_effect=tool),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("agent.chat_completion_helpers._buffer_fallback_notice") as notice,
        patch("hermes_cli.observability.shared_metrics_events.record_fallback") as metric,
    ):
        result = agent.run_conversation("reclaim a better backup if available")

    assert result["final_response"] == "finished"
    assert requested == [original_model, expected_model]
    assert "worse-backup" not in resolved
    assert agent._shared_primary_cooldown_record["reset_at"] == original_cooldown["reset_at"]
    assert agent._shared_primary_cooldown_record["outage_id"] == original_cooldown["outage_id"]
    if expected_model == original_model:
        assert agent.model == original_model
        assert agent.provider == "openai"
    assert time.monotonic() - agent._fallback_stay_started_at < 1
    notice.assert_not_called()
    metric.assert_not_called()


def test_primary_restore_clears_stay_bookkeeping(tool_loop_agent):
    agent = tool_loop_agent
    assert agent._try_activate_fallback(reason=FailoverReason.server_error)
    assert agent._active_fallback_entry is not None
    assert agent._fallback_stay_started_at is not None
    assert agent._restore_primary_runtime()
    assert agent._active_fallback_entry is None
    assert agent._fallback_stay_started_at is None


def test_failed_mid_turn_primary_rebuild_keeps_one_coherent_fallback_route(tool_loop_agent, recovery_runtime):
    # restore_primary_runtime applies primary identity before rebuilding the client and returns
    # False when the rebuild raises. Mid-turn, that must not leave a primary name on the
    # fallback client for the next request.
    agent = tool_loop_agent
    assert agent._try_activate_fallback(reason=FailoverReason.rate_limit)
    agent._rate_limited_until = 0
    fallback = (agent.model, agent.provider, agent.base_url, agent.client)
    with (
        patch("agent.agent_runtime_helpers._refresh_from_shared_cooldown", return_value="expired"),
        patch("agent.agent_runtime_helpers._rebuild_primary_client", side_effect=RuntimeError("rebuild failed")),
    ):
        prompt = recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: backup")

    assert prompt == "Model: backup"
    assert (agent.model, agent.provider, agent.base_url, agent.client) == fallback
    assert agent._fallback_activated and agent._provider_fallback_active
    agent.client.close.assert_not_called()


@pytest.mark.parametrize("failing_step", [
    "agent.client_lifecycle._swap_fallback_clients",
    "agent.chat_completion_helpers._update_fallback_context_compressor",
    "agent.chat_completion_helpers._rescope_fallback_extra_body",
])
def test_fallback_bind_failure_after_client_install_restores_the_original_route(tool_loop_agent, failing_step):
    # A provider-specific setup failure after the swap installed the candidate client must not
    # leave the agent marked as on fallback while pointing at a closed, half-bound route.
    agent = tool_loop_agent
    agent._reasoning_echo_flag = False
    agent._fallback_chain[0]["reasoning_echo"] = True
    original = (agent.model, agent.provider, agent.base_url, agent.api_mode, agent.client)

    compressor = getattr(agent, "context_compressor", None)
    compressor_state = dict(vars(compressor)) if compressor is not None else None

    def swap_then_fail(agent, client, provider, model, base_url, api_mode):
        agent.client = client
        raise RuntimeError("provider setup failed after install")

    def fail(*args, **kwargs):
        if compressor is not None:
            compressor.context_length = 1
        raise RuntimeError("post-bind setup failed")

    side_effect = swap_then_fail if failing_step.endswith("_swap_fallback_clients") else fail
    with patch(failing_step, side_effect=side_effect):
        assert not agent._try_activate_fallback(reason=FailoverReason.server_error)

    if compressor is not None:
        assert vars(compressor) == compressor_state

    assert (agent.model, agent.provider, agent.base_url, agent.api_mode, agent.client) == original
    assert agent._reasoning_echo_flag is False  # the candidate's echo policy must not survive
    assert not agent._fallback_activated
    assert getattr(agent, "_active_fallback_entry", None) is None
    original[-1].close.assert_not_called()


def test_explicit_switch_clears_stay_bookkeeping(tool_loop_agent):
    agent = tool_loop_agent
    assert agent._try_activate_fallback(reason=FailoverReason.server_error)
    from agent.agent_runtime_helpers import _finish_switch
    _finish_switch(agent, "custom", "custom", "custom")
    assert agent._active_fallback_entry is None
    assert agent._fallback_stay_started_at is None


@pytest.mark.parametrize("spare_model", ["muse-wonder-full-1.0", "grok-4.7"])
def test_spare_fallback_returns_to_primary_mid_turn_after_tpm_cooldown(tool_loop_agent, tmp_path, spare_model):
    """A child that 429s onto a spare must not stay there for the rest of one long turn.

    Call 1 hits a TPM 429 on the primary and falls back to the spare; the bounded TPM
    cooldown then expires while the same turn keeps issuing tool calls, so a later call
    in the SAME turn goes back to the primary. A fresh 429 would fall back again normally.
    """
    agent = tool_loop_agent
    agent._fallback_chain[0]["model"] = spare_model
    requested = []
    tool_calls = 0

    def request(kwargs):
        requested.append(agent.model)
        if agent.model == "primary-model" and len(requested) == 1:
            response = httpx.Response(429, request=httpx.Request("POST", "https://primary.invalid/v1"))
            raise RateLimitError(
                "Rate limit reached for gpt in organization on tokens per min (TPM): "
                "Limit 100000, Used 99000, Requested 5000. Please try again in 138h46m.",
                response=response, body={})
        if agent.model == "primary-model":
            return _tool_loop_response()
        return _tool_loop_response(f"spare_call_{len(requested)}")

    def tool(*args, **kwargs):
        nonlocal tool_calls
        tool_calls += 1
        if tool_calls == 2:
            path = tmp_path / "state" / "model_cooldowns.json"
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            # The TPM window was armed at ~60s, not 138h: expire it as the wall clock would.
            for record in state["routes"].values():
                assert float(record["reset_at"]) - time.time() <= 120
                record["reset_at"] = time.time() - 1
            path.write_text(json.dumps(state))
        return "tool result"

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", side_effect=tool),
    ):
        result = agent.run_conversation("long delegated task")

    assert result["final_response"] == "finished"
    assert requested[0] == "primary-model"
    assert requested[1] == spare_model, requested
    assert requested[-1] == "primary-model", requested
    assert agent.model == "primary-model"


def test_child_started_with_cooled_primary_adopts_spare_then_restores_in_same_turn(tool_loop_agent, tmp_path):
    """Pre-dispatch: a primary another process already cooled is never re-probed on call 1,
    and the spare it lands on is still short-lived (restores mid-turn once the cooldown ends)."""
    from agent.shared_primary_cooldown import arm_cooldown, route_from_agent

    agent = tool_loop_agent
    assert arm_cooldown(route_from_agent(agent), reason=FailoverReason.rate_limit,
                        reset_at=time.time() + 60, backoff_count=0, request_id="peer-1")
    requested = []
    tool_calls = 0

    def request(kwargs):
        requested.append(agent.model)
        if agent.model == "primary-model":
            return _tool_loop_response()
        return _tool_loop_response(f"spare_call_{len(requested)}")

    def tool(*args, **kwargs):
        nonlocal tool_calls
        tool_calls += 1
        if tool_calls == 2:
            path = tmp_path / "state" / "model_cooldowns.json"
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            for record in state["routes"].values():
                record["reset_at"] = time.time() - 1
            path.write_text(json.dumps(state))
        return "tool result"

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", side_effect=tool),
    ):
        result = agent.run_conversation("long delegated task")

    assert result["final_response"] == "finished"
    assert requested[0] == "backup", requested
    assert requested[-1] == "primary-model", requested


def test_delegated_child_built_on_a_cooled_role_primary_adopts_a_leased_spare_and_returns(tmp_path, monkeypatch):
    """A child whose role primary another process already cooled is still built on that primary.
    Construction adopts the outage: it walks the chain past a cooled entry, takes the spare's
    concurrency lease, never sends request 1 to the cooled primary, and returns to the role
    primary (releasing the lease) once the cooldown ends inside the same turn."""
    from agent.delegation_context import delegated_child_context
    from agent.shared_primary_cooldown import arm_cooldown, canonical_route, route_from_agent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    primary = ("custom", "https://primary.invalid/v1", "primary-model")
    cooled_backup = {"provider": "openai", "model": "backup", "base_url": "https://backup.invalid/v1"}
    spare = {"provider": "custom:openai-proxy", "model": "muse-wonder-full-1.0", "base_url": "https://spare.invalid/v1"}
    for provider, base_url, model in (primary, (cooled_backup["provider"], cooled_backup["base_url"], cooled_backup["model"])):
        assert arm_cooldown(canonical_route((provider, base_url, model)), reason=FailoverReason.rate_limit,
                            reset_at=time.time() + 60, backoff_count=0, request_id=f"peer-{model}")
    guard_state = tmp_path / "state" / "spare_fallback_guards.json"

    def leases():
        return json.loads(guard_state.read_text(encoding="utf-8"))["leases"] if guard_state.exists() else {}

    tool_defs = [{"type": "function", "function": {
        "name": "web_search", "description": "x", "parameters": {"type": "object", "properties": {}}}}]
    requested = []
    resolved = []
    tool_calls = 0

    def resolve(provider, model=None, **kw):
        resolved.append((model, kw.get("explicit_api_key")))
        return MagicMock(api_key="spare-key", base_url=kw.get("explicit_base_url")), model

    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        delegated_child_context(),
    ):
        agent = AIAgent(
            api_key="role-primary-key", base_url=primary[1], provider=primary[0], model=primary[2],
            api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=[cooled_backup, spare],
        )
        try:
            # Built on the role primary, already serving from the first route that is not cooled.
            assert route_from_agent(agent) == canonical_route(primary)
            assert agent._primary_runtime["model"] == "primary-model"
            assert (agent.model, agent._provider_fallback_active) == ("muse-wonder-full-1.0", True)
            assert agent._spare_fallback_upstream == "meta"
            assert list(leases()["meta"]) == [agent._spare_fallback_lease[1]]
            # The spare is credentialed as its own route: the role primary's key never follows it.
            assert resolved == [("muse-wonder-full-1.0", None)] and agent.api_key == "spare-key"
            assert agent._primary_runtime["api_key"] == "role-primary-key"

            agent._cached_system_prompt = "Model: primary-model\nProvider: custom"
            agent._use_prompt_caching = False
            agent.compression_enabled = False
            agent.save_trajectories = False
            agent.valid_tool_names = {"web_search"}
            agent._api_max_retries = 1
            agent._disable_streaming = True

            def request(kwargs):
                requested.append(agent.model)
                return _tool_loop_response(None if agent.model == "primary-model" else f"spare_call_{len(requested)}")

            def tool(*args, **kwargs):
                nonlocal tool_calls
                tool_calls += 1
                if tool_calls == 2:
                    path = tmp_path / "state" / "model_cooldowns.json"
                    state = json.loads(path.read_text(encoding="utf-8-sig"))
                    for record in state["routes"].values():
                        record["reset_at"] = time.time() - 1
                    path.write_text(json.dumps(state))
                return "tool result"

            with (
                patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
                patch.object(agent, "_cleanup_task_resources"),
                patch.object(agent, "_interruptible_api_call", side_effect=request),
                patch("model_tools.handle_function_call", side_effect=tool),
            ):
                result = agent.run_conversation("long delegated task")
        finally:
            agent.close()

    assert result["final_response"] == "finished"
    assert requested == ["muse-wonder-full-1.0", "muse-wonder-full-1.0", "primary-model"], requested
    assert agent.model == "primary-model" and not agent._provider_fallback_active
    assert agent.api_key == "role-primary-key"
    assert agent._spare_fallback_lease is None and leases() == {}


def test_relapse_still_widens_the_recheck_when_an_activation_attempt_binds_nothing(tool_loop_agent, recovery_runtime):
    """The relapse marker is spent when a fallback takes over, not when one is merely tried:
    an attempt that binds nothing (a spare over its cap, a failed client) must leave the next,
    successful attempt able to see that the primary failed again right after its re-check."""
    agent = tool_loop_agent
    assert agent._try_activate_fallback(FailoverReason.server_error)
    assert agent._uncooled_recheck_delay == 60 and agent._uncooled_restored_at is None

    agent._uncooled_recheck_at = time.monotonic() - 1
    assert recovery_runtime._restore_primary_if_allowed(agent)
    assert agent.model == "primary-model" and agent._uncooled_restored_at is not None

    # The primary fails again at once, and this walk of the chain binds nothing.
    with patch("agent.route_binding.bind_route_entry", side_effect=RuntimeError("client could not be built")):
        assert not agent._try_activate_fallback(FailoverReason.server_error)
    assert agent.model == "primary-model" and not agent._provider_fallback_active
    assert agent._uncooled_restored_at is not None

    agent._fallback_index = 0  # what the next turn's restore does before the chain is walked again
    assert agent._try_activate_fallback(FailoverReason.server_error)
    assert agent.model == "backup"
    # Doubled once for the one relapse: neither reset to the base nor compounded by the retry.
    assert agent._uncooled_recheck_delay == 120
    assert agent._uncooled_recheck_at > time.monotonic() + 60
    assert agent._uncooled_restored_at is None


def _primary_500():
    response = httpx.Response(500, request=httpx.Request("POST", "https://primary.invalid/v1"))
    return InternalServerError("internal server error", response=response, body={})


def test_uncooled_5xx_fallback_returns_to_primary_at_its_bounded_recheck(tool_loop_agent, tmp_path):
    """The production sequence behind the sticky spare: the primary's attempts end on a plain
    5xx, the turn moves to its spare, and because a 5xx arms no cooldown nothing ever sent it
    back. One delegated child then made 189 spare calls over an hour after its primary recovered.
    """
    agent = tool_loop_agent
    requested = []
    seconds_until_recheck = []
    primary_healthy = False
    tool_calls = 0

    def request(kwargs):
        requested.append(agent.model)
        if agent.model == "primary-model":
            if primary_healthy:
                return _tool_loop_response()
            raise _primary_500()
        return _tool_loop_response(f"spare_call_{len(requested)}")

    def tool(*args, **kwargs):
        nonlocal tool_calls, primary_healthy
        tool_calls += 1
        if tool_calls == 3:
            seconds_until_recheck.append(agent._uncooled_recheck_at - time.monotonic())
            # The re-check window elapses while the same turn keeps issuing tool calls.
            agent._uncooled_recheck_at = time.monotonic() - 1
            primary_healthy = True
        return "tool result"

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", side_effect=tool),
    ):
        result = agent.run_conversation("long delegated task")

    assert result["final_response"] == "finished"
    # Not due during the first requests, so they stayed on the spare; one re-check, then primary.
    assert len(seconds_until_recheck) == 1 and 0 < seconds_until_recheck[0] <= 60
    assert requested == ["primary-model"] + ["backup"] * 3 + ["primary-model"]
    assert agent.model == "primary-model" and not agent._provider_fallback_active
    # The re-check is local: it arms no cooldown and writes no shared outage record.
    assert getattr(agent, "_rate_limited_until", 0) == 0
    assert not (tmp_path / "state" / "model_cooldowns.json").exists()


def test_uncooled_recheck_that_fails_again_widens_its_spacing_instead_of_thrashing(tool_loop_agent):
    agent = tool_loop_agent
    requested = []
    backup_calls = 0
    tool_calls = 0

    def request(kwargs):
        nonlocal backup_calls
        requested.append(agent.model)
        if agent.model == "primary-model":
            raise _primary_500()
        backup_calls += 1
        return _tool_loop_response(f"relapse_call_{backup_calls}" if backup_calls < 5 else None)

    def tool(*args, **kwargs):
        nonlocal tool_calls
        tool_calls += 1
        if tool_calls == 2:
            agent._uncooled_recheck_at = time.monotonic() - 1
        return "tool result"

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=request),
        patch("model_tools.handle_function_call", side_effect=tool),
    ):
        result = agent.run_conversation("primary is still down")

    assert result["final_response"] == "finished"
    # One re-check, one failed primary request, then the spare for the rest of the turn.
    assert requested == ["primary-model", "backup", "backup", "primary-model", "backup", "backup", "backup"]
    assert agent.model == "backup"
    assert agent._uncooled_recheck_delay == 120
    assert agent._uncooled_recheck_at > time.monotonic() + 60


def test_uncooled_recheck_is_attempted_once_per_window(recovery_runtime):
    from agent.fallback_route_recovery_uncooled import (
        UNCOOLED_RECHECK_BASE_S, UNCOOLED_RECHECK_CAP_S, begin_uncooled_episode, settle_uncooled_recheck,
    )

    restore = Mock(return_value=False)
    agent = SimpleNamespace(
        _fallback_activated=True, _provider_fallback_active=True, _fallback_episode_cooled=True,
        _cached_system_prompt="Model: primary", _restore_primary_runtime=restore,
    )
    begin_uncooled_episode(agent, FailoverReason.server_error)
    assert agent._fallback_episode_cooled is False

    # Before the deadline no request probes the primary.
    assert recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: fallback") == "Model: fallback"
    restore.assert_not_called()

    # At the deadline exactly one probe runs; a refused restore waits a full window again.
    agent._uncooled_recheck_at = time.monotonic() - 1
    for _ in range(3):
        assert recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: fallback") == "Model: fallback"
    restore.assert_called_once_with()
    assert agent._uncooled_recheck_at > time.monotonic() + UNCOOLED_RECHECK_BASE_S - 5

    # Repeated relapses double the spacing up to the cap; a primary that held starts over.
    for _ in range(8):
        settle_uncooled_recheck(agent, restored=True)
        begin_uncooled_episode(agent, FailoverReason.timeout)
    assert agent._uncooled_recheck_delay == UNCOOLED_RECHECK_CAP_S
    agent._uncooled_restored_at = time.monotonic() - 2 * UNCOOLED_RECHECK_CAP_S
    begin_uncooled_episode(agent, None)
    assert agent._uncooled_recheck_delay == UNCOOLED_RECHECK_BASE_S


def test_deterministic_refusal_fallback_stays_turn_scoped(recovery_runtime):
    from agent.fallback_route_recovery_uncooled import begin_uncooled_episode

    restore = Mock(return_value=True)
    agent = SimpleNamespace(
        _fallback_activated=True, _provider_fallback_active=True,
        _cached_system_prompt="Model: primary", _restore_primary_runtime=restore,
    )
    # The primary would refuse the same conversation the same way on every re-check.
    begin_uncooled_episode(agent, FailoverReason.content_policy_blocked)
    assert agent._uncooled_recheck_at is None
    assert recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: fallback") == "Model: fallback"
    restore.assert_not_called()
