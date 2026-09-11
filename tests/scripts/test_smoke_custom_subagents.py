"""Public result-identity contracts used by the custom-subagent smoke."""

import pytest

from scripts.smoke_custom_subagents import _result_for_task


def test_result_join_uses_emitted_task_index_not_unpublished_role():
    reader = _result_for_task([
        {"task_index": 1, "summary": "worker"},
        {"task_index": 0, "summary": "reader"},
    ], 0, "reader")
    assert reader["summary"] == "reader"


@pytest.mark.parametrize("results, match", [
    ([{"task_index": 1}], "identity missing"),
    ([{"task_index": 0}, {"task_index": 0}], "identity duplicate"),
])
def test_result_join_rejects_missing_or_duplicate_task_identity(results, match):
    with pytest.raises(RuntimeError, match=match):
        _result_for_task(results, 0, "reader")


def test_active_route_uses_complete_configured_runtime(monkeypatch):
    from scripts.smoke_custom_subagents import _resolve_active_credentials
    from hermes_cli import runtime_provider
    configured = {'provider': 'custom-route', 'api_mode': 'chat_completions',
                  'api_key': 'synthetic-key', 'base_url': 'https://custom.invalid/v1'}
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: {
        'model': {'default': 'custom-model', 'provider': 'custom-route'}})
    calls = []
    def resolve(**kwargs):
        calls.append(kwargs)
        return configured
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', resolve)
    runtime, source = _resolve_active_credentials()
    assert runtime == {**configured, 'model': 'custom-model'}
    assert calls == [{'requested': 'custom-route', 'target_model': 'custom-model'}]
    assert source == 'hermes_cli.runtime_provider.resolve_runtime_provider'
