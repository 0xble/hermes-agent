"""User-visible session controls through plugin discovery and real dispatch."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli.session_model import request_session_model, session_model_scope


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [session-model]\n")
    return tmp_path


@pytest.fixture
def agent(home):
    return SimpleNamespace(session_id="session-a", model="gpt-5.6", provider="openai-codex",
        api_mode="codex_responses", base_url="https://chatgpt.com/backend-api/codex",
        api_key="", reasoning_config={"effort": "medium", "enabled": True})


def request(**args):
    return json.loads(request_session_model(args, task_id="session-a"))


def test_discovery_and_registry_dispatch(home, agent):
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["session-model"]
    assert loaded.enabled, loaded.error
    entry = registry.get_entry("session_model", scope=manager.scope_key)
    assert entry is not None
    applied = []
    with session_model_scope(agent, applied.append) as control:
        result = json.loads(entry.handler({"reasoning": "high"}, task_id=agent.session_id))
        assert result["status"] == "queued"
        assert agent.reasoning_config["effort"] == "medium"
        assert not applied
        outcome = control.finish({"completed": True, "final_response": "Queued."})
    assert outcome["session_model"]["status"] == "applied"
    assert applied[0].reasoning["effort"] == "high"
    assert "api_key" not in result


@pytest.mark.parametrize("args", [{}, {"model": ""}, {"reasoning": None}, {"reasoning": "hide"},
    {"provider": "anthropic"}, {"model": "x --global"}, {"reasoning": " high"},
    {"model": "x", "global": True}, {"reasoning": "ultra"}])
def test_invalid_settings_do_not_queue(agent, args):
    apply = Mock()
    with session_model_scope(agent, apply) as control:
        assert request(**args)["status"] == "rejected"
        control.finish({"completed": True})
    apply.assert_not_called()


def test_scope_and_child_isolation(agent):
    assert request(reasoning="high")["status"] == "rejected"
    with session_model_scope(agent, Mock(), allowed=False):
        assert request(reasoning="high")["status"] == "rejected"
    with session_model_scope(agent, Mock()):
        result = json.loads(request_session_model({"reasoning": "high"}, task_id="child"))
        assert result["status"] == "rejected"


@pytest.mark.parametrize("result", [{"completed": False}, {"completed": True, "interrupted": True},
    {"completed": True, "failed": True}])
def test_failed_turn_cancels(agent, result):
    apply = Mock()
    with session_model_scope(agent, apply) as control:
        assert request(reasoning="high")["status"] == "queued"
        assert control.finish(result)["session_model"]["status"] == "cancelled"
    apply.assert_not_called()


def test_concurrent_settings_change_and_duplicate_request(agent):
    apply = Mock()
    with session_model_scope(agent, apply) as control:
        assert request(reasoning="high")["status"] == "queued"
        assert request(reasoning="low")["status"] == "rejected"
        agent.reasoning_config = {"effort": "low"}
        assert control.finish({"completed": True})["session_model"]["status"] == "failed"
    apply.assert_not_called()


def test_apply_failure_receipt(agent):
    with session_model_scope(agent, Mock(side_effect=ValueError("client initialization failed"))) as control:
        assert request(reasoning="high")["status"] == "queued"
        result = control.finish({"completed": True})
    assert result["session_model"]["status"] == "failed"
    assert agent.reasoning_config["effort"] == "medium"


def test_combined_request_is_atomic_and_preserves_omitted_fields(agent, monkeypatch):
    from hermes_cli.model_switch import ModelSwitchResult
    route = ModelSwitchResult(success=True, new_model="gpt-5.6-sol", target_provider="openai-codex",
        api_mode="codex_responses", base_url=agent.base_url)
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kwargs: route)
    monkeypatch.setattr("hermes_cli.model_selection_guards.combined_selection_warning", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.context_switch_guard.merge_preflight_compression_warning", lambda *a, **k: None)
    applied = []
    with session_model_scope(agent, applied.append) as control:
        assert request(model="gpt-5.6-sol", reasoning="ultra")["status"] == "rejected"
        assert not applied
        assert agent.model == "gpt-5.6"
        assert request(model="gpt-5.6-sol")["status"] == "queued"
        control.finish({"completed": True})
    assert applied[0].reasoning == {"effort": "medium", "enabled": True}
    assert applied[0].route.new_model == "gpt-5.6-sol"


def test_plugin_disabled_by_default(home):
    from hermes_cli.plugins import PluginManager
    (home / "config.yaml").write_text("{}")
    manager = PluginManager()
    manager.discover_and_load()
    assert not manager._plugins["session-model"].enabled


def test_nested_session_does_not_leak(agent):
    import copy
    other = copy.copy(agent)
    other.session_id = "session-b"
    outer = []
    with session_model_scope(agent, outer.append) as control:
        with session_model_scope(other, Mock()):
            assert request(reasoning="high")["status"] == "rejected"
        assert request(reasoning="high")["status"] == "queued"
        control.finish({"completed": True})
    assert outer[0].reasoning["effort"] == "high"


def test_anthropic_mandatory_thinking(agent):
    agent.model = "claude-fable-5-1"
    agent.provider = "anthropic"
    agent.api_mode = "anthropic_messages"
    with session_model_scope(agent, Mock()):
        assert request(reasoning="none")["status"] == "rejected"
        assert request(reasoning="high")["status"] == "queued"


@pytest.mark.parametrize('completion', ['applied', 'failed', 'cancelled'])
def test_receipt_matches_durable_terminal_history(agent, tmp_path, completion):
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'receipt.db')
    try:
        db.create_session(agent.session_id, source='cli')
        messages = [{'role': 'user', 'content': 'Set reasoning high'},
                    {'role': 'assistant', 'content': 'Queued.'}]
        db.replace_messages(agent.session_id, messages)
        agent._session_db = db
        agent._session_messages = messages
        apply = Mock(side_effect=ValueError('rejected')) if completion == 'failed' else Mock()
        with session_model_scope(agent, apply) as control:
            assert request(reasoning='high')['status'] == 'queued'
            result = control.finish({'completed': completion != 'cancelled',
                                     'messages': messages, 'final_response': 'Queued.'})
        assert result['session_model']['status'] == completion
        assert result['messages'][-1]['content'] == result['final_response']
        assert agent._session_messages[-1]['content'] == result['final_response']
        assert db.get_messages(agent.session_id)[-1]['content'] == result['final_response']
    finally:
        db.close()
