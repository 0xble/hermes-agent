"""Selection admission and configured parent route through the evaluation runner."""
import json
from types import SimpleNamespace

import pytest

from scripts import eval_delegation_selection as evaluation


@pytest.mark.parametrize("selector", ["misspelled", ""])
def test_invalid_selector_fails_before_credentials_or_fixture_setup(monkeypatch, selector):
    monkeypatch.setattr("hermes_cli.auth.resolve_codex_runtime_credentials",
                        lambda: pytest.fail("resolved Codex auth"))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: pytest.fail("loaded live config"))
    monkeypatch.setattr(evaluation, "_setup_home", lambda: pytest.fail("created fixture"))
    with pytest.raises(SystemExit, match="scenario"):
        evaluation.run(selector)


def test_configured_parent_route_reaches_agent_before_fixture_override(tmp_path, monkeypatch):
    from hermes_cli import runtime_provider
    from tools import delegate_tool
    events = []
    runtime = {"provider": "custom", "api_mode": "chat_completions",
               "base_url": "https://custom.invalid/v1", "api_key": "synthetic-secret",
               "request_overrides": {"extra_headers": {"X-Test": "sentinel"}},
               "max_output_tokens": 1234}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "model": {"default": "custom-model", "provider": "custom"},
        "agent": {"reasoning_effort": "low"}})
    def resolve(**kwargs):
        assert kwargs == {"requested": "custom", "target_model": "custom-model"}
        events.append("resolve")
        return runtime
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    monkeypatch.setattr("hermes_cli.auth.resolve_codex_runtime_credentials",
                        lambda: pytest.fail("resolved unrelated Codex auth"))
    def setup():
        assert events == ["resolve"]
        events.append("setup")
        return tmp_path
    monkeypatch.setattr(evaluation, "_setup_home", setup)
    captured = []
    class Parent:
        session_id = "fixture"
        def __init__(self, **kwargs):
            captured.append(kwargs)
        def run_conversation(self, prompt):
            return {"completed": True, "final_response": "2"}
        def close(self):
            pass
    monkeypatch.setattr("run_agent.AIAgent", Parent)
    monkeypatch.setattr("hermes_state.SessionDB", lambda: SimpleNamespace(
        create_session=lambda *a, **k: None, close=lambda: None))
    original = delegate_tool.delegate_task
    receipt = evaluation.run("keep_simple_work_local")
    assert receipt["total"] == 1
    assert delegate_tool.delegate_task is original
    for key, value in runtime.items():
        assert captured[0]["max_tokens" if key == "max_output_tokens" else key] == value
    assert captured[0]["model"] == "custom-model"
    assert captured[0]["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert "synthetic-secret" not in json.dumps(receipt)
    assert receipt["credential_source"] == "hermes_cli.runtime_provider.resolve_runtime_provider"


def test_cli_rejects_unknown_scenario_without_starting_runtime(monkeypatch):
    monkeypatch.setattr("sys.argv", ["eval_delegation_selection.py", "--only", "not-a-scenario"])
    monkeypatch.setattr(evaluation, "_setup_home", lambda: pytest.fail("fixture started"))
    with pytest.raises(SystemExit) as exc:
        evaluation.main()
    assert exc.value.code != 0
