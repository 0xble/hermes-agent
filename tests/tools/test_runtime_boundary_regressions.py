"""Observable runtime boundaries; no provider, credential or user-state writes."""
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pytest

from tools import delegate_tool as dt
from tools import delegate_tool_child_run as cr
from tools import subagent_worktree as sw
from tools.delegate_tool_config import _get_worktree_isolation
from tools.delegation_history import portable_history
from tests.tools.test_worktree_isolation_modes import repo_at


def test_invalid_isolation_mode_is_not_silent_disable(monkeypatch):
    monkeypatch.setattr(dt, '_load_config', lambda: {'worktree_isolation': 'requried'})
    with pytest.raises(ValueError, match='worktree_isolation'):
        _get_worktree_isolation()


@pytest.mark.parametrize('mode,local,expected', [(False, True, 'disabled'), (True, False, 'skipped'), ('required', False, 'failed'), (True, True, 'skipped'), ('required', True, 'failed')])
def test_child_launch_outcome_and_call_trace(tmp_path, monkeypatch, mode, local, expected):
    monkeypatch.setattr(dt, '_load_config', lambda: {'worktree_isolation': mode})
    monkeypatch.setattr(sw, 'local_backend_active', lambda: local)
    monkeypatch.setattr(dt, '_resolve_workspace_hint', lambda parent: str(tmp_path))
    monkeypatch.setattr('tools.terminal_tool.get_session_cwd', lambda tid: str(tmp_path))
    child = SimpleNamespace(run_conversation=Mock(return_value={'completed': True, 'final_response': 'inspected', 'messages': []}), close=Mock(), session_id='boundary-child')
    result = dt._run_single_child(0, 'Inspect only', child=child)
    assert result['worktree_isolation']['state'] == expected, result
    assert result['worktree_isolation']['reason']
    assert child.run_conversation.call_count == (0 if mode == 'required' else 1)
    assert (result['status'] == 'completed') == (mode != 'required')


def test_explicit_invalid_anchor_never_redirects(tmp_path, monkeypatch):
    valid = repo_at(tmp_path / 'valid')
    monkeypatch.setattr(dt, '_load_config', lambda: {'worktree_isolation': 'required', 'worktree_repo_root': str(tmp_path / 'missing')})
    monkeypatch.setattr(sw, 'local_backend_active', lambda: True)
    monkeypatch.setattr(dt, '_resolve_workspace_hint', lambda parent: str(valid))
    monkeypatch.setattr('tools.terminal_tool.get_session_cwd', lambda tid: str(valid))
    receipt = {}
    with pytest.raises(cr.WorktreeIsolationRequiredError):
        cr._create_isolated_worktree(None, None, 'invalid', receipt)
    assert receipt['reason'] == 'invalid_explicit_repo_root'
    assert not (valid / '.worktrees').exists()


def test_linked_worktree_exclude_does_not_mutate_tracked_files(tmp_path):
    repo = repo_at(tmp_path / 'repo')
    first = sw.create_subagent_worktree(str(repo), 'first')
    assert first is not None
    second = sw.create_subagent_worktree(first['path'], 'nested')
    assert second and sw.is_linked_worktree(second['path'])
    assert not (repo / '.gitignore').exists()
    assert not (tmp_path / 'repo' / '.worktrees' / 'subagent-first' / '.gitignore').exists()
    assert sw._run_git(['status', '--porcelain'], str(repo)).stdout == ''
    sw.finalize_subagent_worktree(second)
    sw.finalize_subagent_worktree(first)


def test_schema_retry_keeps_child_authority_and_session(monkeypatch):
    from agent.delegation_context import is_dispatcher_owned_worker_context, is_read_only_knowledge_context
    from gateway.session_context import get_session_env, scoped_current_session_id
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'parent-owned-card')
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    observations = []
    def retry(**kwargs):
        observations.append((is_dispatcher_owned_worker_context(), is_read_only_knowledge_context(), get_session_env("HERMES_SESSION_ID")))
        return {'final_response': '{"ok": true}', 'api_calls': 1}
    child = SimpleNamespace(session_id='retry-child', memory_access_mode='read_only', _delegate_output_schema={'type': 'object', 'required': ['ok']}, run_conversation=retry)
    with scoped_current_session_id('parent-session'):
        outcome = cr._validate_child_output_schema(child, {'final_response': 'invalid'}, 0, 'task', None)
        assert get_session_env("HERMES_SESSION_ID") == 'parent-session'
    assert observations == [(False, True, 'retry-child')]
    assert outcome.valid is True and outcome.retries == 1


def test_required_resume_cannot_run_after_cwd_seed_failure(tmp_path, monkeypatch):
    repo = repo_at(tmp_path / 'repo')
    info = sw.create_subagent_worktree(str(repo), 'resume')
    assert info
    monkeypatch.setattr(dt, '_load_config', lambda: {'worktree_isolation': 'required'})
    def fail_seed(*args):
        raise OSError('seed unavailable')
    monkeypatch.setattr('tools.terminal_tool.record_session_cwd', fail_seed)
    child = SimpleNamespace(_delegation_resume_workspace_path=info['path'],
        run_conversation=Mock(return_value={'completed': True, 'final_response': 'unsafe'}),
        close=Mock(), session_id='resume-child')
    result = dt._run_single_child(0, 'Continue', child=child)
    assert child.run_conversation.call_count == 0
    assert result['worktree_isolation']['state'] == 'failed'
    assert 'cwd_seed_failed' in result['worktree_isolation']['reason']
    sw.finalize_subagent_worktree(info)


def test_fork_quotes_authorization_and_never_replays_tools():
    rows = [
        {'role': 'system', 'content': 'PARENT_ONLY_PERMISSION'},
        {'role': 'developer', 'content': 'SECRET_PARENT_POLICY'},
        {'role': 'user', 'content': 'Prior request: stop sibling sa-foreign and publish code.'},
        {'role': 'assistant', 'tool_calls': [{'id': 'old', 'function': {'name': 'delegate_task', 'arguments': '{"action":"stop","subagent_id":"sa-foreign"}'}}]},
        {'role': 'tool', 'tool_call_id': 'old', 'content': 'old action result'},
    ]
    fork = portable_history(rows)
    assert len(fork) == 1 and fork[0]['role'] == 'user'
    assert 'PARENT_ONLY_PERMISSION' not in str(fork) and 'SECRET_PARENT_POLICY' not in str(fork)
    assert 'not new instructions, permission grants' in fork[0]['content']
    assert 'tool_calls' not in fork[0] and 'tool_call_id' not in fork[0]
    quoted = json.loads(fork[0]['content'].split('\n')[-1])
    assert quoted[1]['call']['name'] == 'delegate_task'
    assert quoted[2]['content'] == 'old action result'


def test_429_result_does_not_claim_plan_or_demote_model():
    child = SimpleNamespace(model='frontier-planner', provider='planning-provider', session_id='planner')
    result = {'failed': True, 'failure_reason': 'rate_limit', 'error': 'HTTP 429', 'final_response': 'Provider unavailable', 'api_calls': 1}
    entry = cr._build_result_entry(child, result, 0, 1.0, cr._SchemaOutcome(None, None, [], 0))
    assert entry['status'] == 'failed'
    assert entry['model'] == 'frontier-planner'
    assert entry['route_transitions'] == []
    assert entry['exit_reason'] == 'error'


@pytest.mark.parametrize("mode,state", [(False, "disabled"), (True, "skipped"), ("required", "failed")])
def test_nonlinked_resume_reports_configured_mode(tmp_path, monkeypatch, mode, state):
    repo = repo_at(tmp_path / "resume-repo")
    monkeypatch.setattr(dt, "_load_config", lambda: {"worktree_isolation": mode})
    child = SimpleNamespace(_delegation_resume_workspace_path=str(repo),
        run_conversation=Mock(return_value={"completed": True, "final_response": "resumed"}),
        close=Mock(), session_id="resume-ordinary")
    result = dt._run_single_child(0, "Continue", child=child)
    assert result["worktree_isolation"]["state"] == state
    assert result["worktree_isolation"]["reason"] == ("disabled" if mode is False else "resume_workspace_not_linked")
    assert child.run_conversation.call_count == (0 if mode == "required" else 1)
