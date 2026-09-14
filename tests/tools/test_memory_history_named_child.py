"""Memory rollback obeys named-child authority through actual worker/tool paths."""
import json
from pathlib import Path
import shlex
import sys

import pytest

from tests.agent.test_custom_subagent_runtime import make_child


@pytest.mark.parametrize('entry', ['direct', 'execute_code', 'terminal'])
@pytest.mark.parametrize('named', [True, False])
def test_history_rollback_obeys_actual_child_execution_authority(make_child, monkeypatch, entry, named):
    from agent.delegation_context import is_read_only_knowledge_context
    from tools import code_kernel, delegate_tool
    from tools.custom_subagents import parse_definitions
    from tools.delegate_tool_child_run import _ChildRun
    from tools.memory_history import HistoryTransaction, list_history, rollback
    from tools.memory_tool_store import MemoryStore
    from tools.terminal_tool import cleanup_all_environments

    cleanup_all_environments()
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr('tools.code_execution_tool._load_config', lambda: {'mode': 'strict', 'timeout': 15})
    monkeypatch.setattr('tools.code_execution_tool._resolve_child_python', lambda mode: sys.executable)
    store = MemoryStore()
    store.load_from_disk()
    assert store.add('memory', 'before')['success']
    operations = [{'action': 'replace', 'old_text': 'before', 'content': 'after'}]
    assert store.apply_batch('memory', operations, transaction=HistoryTransaction('memory', operations))['success']
    record_id = list_history()[0]['id']
    memory_path = store._path_for('memory')
    before_bytes = memory_path.read_bytes()
    parent = make_child()
    del parent._delegation_runtime_pin
    definition = parse_definitions({'subagents': {'reader': {
        'description': 'Read fixture', 'instructions': 'Report evidence.',
        'provider': 'openai-codex', 'model': parent.model, 'reasoning_effort': 'medium',
    }}})['reader'] if named else None
    child = delegate_tool._build_child_agent(
        task_index=0, goal='Inspect the fixture.', context=None, toolsets=['terminal', 'code_execution'],
        model=parent.model, max_iterations=1, task_count=1, parent_agent=parent,
        subagent_definition=definition, resolved_reasoning={'enabled': True, 'effort': 'medium'} if named else None)
    observed = {}
    repo = str(Path(__file__).resolve().parents[2])

    def execute(**kwargs):
        observed['read_only'] = is_read_only_knowledge_context()
        if entry == 'direct':
            observed['result'] = rollback(record_id, store)
        elif entry == 'execute_code':
            from tools.code_execution_tool import execute_code
            code = (f'import sys; sys.path.insert(0, {repo!r}); '
                    'import json; from tools.memory_history import list_history, rollback; '
                    'from tools.memory_tool_store import MemoryStore; '
                    'store=MemoryStore(); store.load_from_disk(); '
                    'print(json.dumps(rollback(list_history()[0]["id"], store)))')
            result = json.loads(execute_code(code, task_id=child.session_id))
            assert result['status'] == 'success', result
            observed['result'] = json.loads(result['output'])
        else:
            from tools.terminal_tool import terminal_tool
            command = f'{shlex.quote(sys.executable)} -m tools.memory_history rollback {record_id}'
            result = json.loads(terminal_tool(command, task_id=child.session_id, workdir=repo, timeout=15, _allow_yield=False))
            observed['result'] = json.loads(result['output'])
            observed['exit_code'] = result['exit_code']
        return {'completed': True, 'final_response': 'Fixture complete.'}

    monkeypatch.setattr(child, 'run_conversation', execute)
    code_kernel.shutdown_all_kernels()
    try:
        result, error, deferred = _ChildRun(child, parent, 0, 'Inspect fixture.', None, None,
                                           child_task_id=child.session_id).await_child()
        assert error is None, error
        assert result['completed'] and not deferred
        assert observed['result']['success'] is (not named), observed
        assert observed['read_only'] is named
        if named:
            assert 'parent-owned' in observed['result']['error']
            assert memory_path.read_bytes() == before_bytes
            assert list_history()[0]['status'] == 'applied'
            assert not is_read_only_knowledge_context()
            assert rollback(record_id, store)['success']
        assert memory_path.read_text() == 'before'
    finally:
        code_kernel.shutdown_all_kernels()
        cleanup_all_environments()
        child.close()
