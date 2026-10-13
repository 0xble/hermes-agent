"""Regression tests for restoring a rate-limited primary during a long turn."""

import ast
import inspect
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
from openai import RateLimitError

from agent import conversation_loop, turn_iteration_prep
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
        _fallback_activated=True,
        _cached_system_prompt="Model: primary\nProvider: primary-provider",
        _restore_primary_runtime=restore,
    )

    result = recovery_runtime._restore_primary_runtime_for_iteration(agent, "Model: fallback")

    assert result == agent._cached_system_prompt
    restore.assert_called_once_with()


def test_keeps_fallback_prompt_when_cooldown_has_not_expired(recovery_runtime):
    restore = Mock(return_value=False)
    agent = SimpleNamespace(
        _fallback_activated=True,
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


def test_outer_iteration_probes_before_building_the_next_request():
    tree = ast.parse(inspect.getsource(conversation_loop._run_conversation_turn))
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_run_phase" and node.args
        and isinstance(node.args[0], ast.Name) and node.args[0].id == "begin_iteration"
        for node in ast.walk(tree)
    ), "the conversation loop must execute the iteration entry phase"
    entry = ast.parse(inspect.getsource(turn_iteration_prep.begin_iteration))
    restore_lines = [
        node.lineno for node in ast.walk(entry)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_maybe_recheck_fallback_route_for_iteration"
    ]
    redirect_lines = [
        node.lineno for node in ast.walk(entry)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_drain_pending_redirect"
    ]
    assert restore_lines, "the outer iteration must re-check primary restoration"
    assert redirect_lines, "expected normal outer-loop request setup"
    assert min(restore_lines) < min(redirect_lines)


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
            assert not path.exists()
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
        _provider_fallback_active=True, model="last-resort", provider="backup",
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

    agent._try_activate_fallback = Mock(side_effect=fail)
    if failure == "restore":
        agent._restore_primary_runtime.side_effect = RuntimeError("shared read failure")
    assert recovery_runtime._maybe_recheck_fallback_route_for_iteration(agent, "old prompt") == "old prompt"
    assert agent.client is original_client
    assert agent.model == compressor.model == "last-resort"
    assert agent._cached_system_prompt == "Model: last-resort"
    assert agent._fallback_index == 2
    assert time.monotonic() - agent._fallback_stay_started_at < 1


@pytest.mark.parametrize("value", [None, 0, -1, False, "bad", float("nan"), float("inf")])
def test_unset_or_invalid_stay_limit_does_not_rewalk_chain(value, recovery_runtime):
    agent = SimpleNamespace(
        _fallback_activated=True, _fallback_index=1,
        _fallback_chain=[{"provider": "backup", "model": "last", "max_stay_seconds": value}],
        _fallback_stay_started_at=time.monotonic() - 100,
        _restore_primary_runtime=Mock(return_value=False),
        _try_activate_fallback=Mock(),
    )
    assert recovery_runtime._maybe_recheck_fallback_route_for_iteration(agent, "old prompt") == "old prompt"
    agent._restore_primary_runtime.assert_called_once()
    agent._try_activate_fallback.assert_not_called()
