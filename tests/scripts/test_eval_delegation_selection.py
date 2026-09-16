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


@pytest.mark.parametrize("roles_source", ["fixture", "configured"])
@pytest.mark.parametrize("break_interception", [None, "registry", "dedicated"])
def test_configured_parent_route_reaches_agent_before_fixture_override(tmp_path, monkeypatch, roles_source, break_interception):
    from hermes_cli import runtime_provider
    from tools import delegate_tool
    events = []
    runtime = {"provider": "custom", "api_mode": "chat_completions",
               "base_url": "https://custom.invalid/v1", "api_key": "synthetic-secret",
               "request_overrides": {"extra_headers": {"X-Test": "sentinel"}},
               "max_output_tokens": 1234}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "model": {"default": "custom-model", "provider": "custom"},
        "agent": {"reasoning_effort": "low"},
        "delegation": {"subagents": {"actual": {"description": "Live policy", "context_mode": "fresh"}}}})
    def resolve(**kwargs):
        assert kwargs == {"requested": "custom", "target_model": "custom-model"}
        events.append("resolve")
        return runtime
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    monkeypatch.setattr("hermes_cli.auth.resolve_codex_runtime_credentials",
                        lambda: pytest.fail("resolved unrelated Codex auth"))
    def setup(roles=None, defaults=None):
        if roles_source == "configured":
            assert roles == {"actual": {"description": "Live policy", "context_mode": "fresh"}}
        assert events == ["resolve"]
        events.append("setup")
        (tmp_path / "notes.txt").write_text("teh example\n")
        return tmp_path
    monkeypatch.setattr(evaluation, "_setup_home", setup)
    captured = []
    class Parent:
        session_id = "fixture"
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self.provider = kwargs["provider"]
            self.model = kwargs["model"]
            self.api_mode = kwargs["api_mode"]
        def run_conversation(self, prompt):
            if break_interception == "registry":
                registry.dispatch = lambda *a, **kw: "{}"
            elif break_interception == "dedicated":
                self._dispatch_delegate_task = lambda *a, **kw: "{}"
            return {"completed": True, "final_response": "2",
                    "messages": [{"role": "user", "content": prompt}]}
        def close(self):
            pass
    monkeypatch.setattr("run_agent.AIAgent", Parent)
    monkeypatch.setattr("hermes_state.SessionDB", lambda **kwargs: SimpleNamespace(
        create_session=lambda *a, **k: None, close=lambda: None))
    from tools.registry import registry
    original_dispatch = registry.dispatch
    original = delegate_tool.delegate_task
    selector = "simple_typo_stays_direct" if roles_source == "configured" else "keep_simple_work_local"
    receipt = evaluation.run(selector, roles_source=roles_source)
    assert receipt["total"] == 1
    assert receipt["roles_source"] == roles_source
    assert receipt["verdicts"][0]["interception_safety"]["passed"] == (not break_interception)
    if break_interception:
        assert receipt["verdicts"][0]["harness_failure"]
        assert not receipt["verdicts"][0]["inconclusive"]
    assert captured[0]["max_iterations"] == (6 if roles_source == "configured" else 16)
    assert set(receipt["role_catalog"]) == ({"actual"} if roles_source == "configured" else set(evaluation.ROLES))
    assert delegate_tool.delegate_task is original
    assert registry.dispatch == original_dispatch
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


@pytest.mark.parametrize('named_role', ['explorer', 'worker'])
@pytest.mark.parametrize('legacy', [True, False])
def test_judge_named_role_matches_actual_delegation_preflight(monkeypatch, named_role, legacy):
    from tools import custom_subagents, delegate_tool

    # Keep real normalization, definition selection and scoring. Only the
    # credential boundary is synthetic: this contract test never starts a model.
    monkeypatch.setattr(custom_subagents, 'resolve_named_credentials',
                        lambda definition, *a: ({'provider': 'fixture', 'model': definition.model}, None))
    monkeypatch.setattr(custom_subagents, 'freeze_fallback_routes', lambda *a, **kw: ())
    goal = 'Inspect src/retry.py and return evidence identifying the retry implementation.'
    call = ({'goal': goal, 'role': named_role} if legacy else {
        'tasks': [{'goal': goal, 'subagent_type': named_role}]})
    tasks, error = delegate_tool._normalize_task_list(
        call.get('goal'), None, call.get('tasks'), None,
        delegate_tool._normalize_role(call.get('role')), 3)
    assert error is None
    launches, error = delegate_tool._preflight_task_runtime(
        tasks, {'subagents': evaluation.ROLES}, None, None,
        {'provider': 'fixture', 'model': 'legacy'})
    assert error is None
    selected = launches[0].definition
    assert (selected.name if selected else None) == (None if legacy else named_role)

    verdict = evaluation._judge(
        {'name': 'named-role-contract', 'why': 'Configured role selection must be real.',
         'expect': {'delegated': True, 'roles': [named_role]}},
        [call], turn={'completed': True}, error=None)
    assert verdict['roles'] == [None if legacy else named_role]
    assert verdict['findings']['role_choice'] is (not legacy)
    assert verdict['passed'] is (not legacy)


@pytest.mark.parametrize("failure", ["constructor", "conversation", "sqlite", "close"])
def test_runner_cleanup_and_sanitized_diagnostics(tmp_path, monkeypatch, failure):
    from tools import delegate_tool
    from tools.registry import registry
    original_dispatch, original_delegate = registry.dispatch, delegate_tool.delegate_task
    closed = []
    (tmp_path / "notes.txt").write_text("teh example")
    monkeypatch.setattr(evaluation, "_setup_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "model": {"default": "test-model", "provider": "custom"}})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
        "provider": "custom", "api_mode": "chat_completions", "api_key": "secret-value"})
    monkeypatch.setattr("hermes_state.SessionDB", lambda **kw: SimpleNamespace(
        create_session=lambda *a, **k: None, close=lambda: closed.append("db")))
    class Parent:
        session_id = "fixture"
        provider, model, api_mode = "custom", "test-model", "chat_completions"
        def __init__(self, **kwargs):
            if failure == "constructor": raise RuntimeError("construction failed")
        def run_conversation(self, prompt):
            if failure == "conversation":
                raise RuntimeError("HTTP 429 secret-value https://private.invalid/?token=secret-value")
            if failure == "sqlite": return {}
            return {"completed": True, "final_response": "2 lines",
                    "messages": [{"role": "user", "content": prompt}]}
        def close(self):
            closed.append("parent")
            if failure == "close": raise RuntimeError("close failed token=secret-value")
    monkeypatch.setattr("run_agent.AIAgent", Parent)
    if failure == "constructor":
        with pytest.raises(RuntimeError, match="construction failed"):
            evaluation.run("keep_simple_work_local")
        assert closed == ["db"]
    else:
        receipt = evaluation.run("keep_simple_work_local")
        assert closed == ["parent", "db"]
        assert not receipt["verdicts"][0]["passed"]
        encoded = json.dumps(receipt)
        assert "secret-value" not in encoded and "private.invalid" not in encoded
        assert receipt["verdicts"][0]["diagnostics"]
        if failure == "conversation":
            assert "HTTP 429" in receipt["verdicts"][0]["turn_error"]
    assert registry.dispatch == original_dispatch
    assert delegate_tool.delegate_task is original_delegate
