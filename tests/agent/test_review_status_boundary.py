from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.review_policy import filter_child_tool_snapshot, remove_parent_only_review_tools


def test_child_exclusion_preserves_staged_name_only_tools():
    agent = SimpleNamespace(tools=[{'name': 'read_file'}], valid_tool_names={'read_file', 'terminal', 'review_current_work'})
    remove_parent_only_review_tools(agent)
    assert agent.valid_tool_names == {'read_file', 'terminal'}
    defs, names = filter_child_tool_snapshot(SimpleNamespace(), [{'name':'read_file'}], {'read_file','engine_tool'})
    assert names == {'read_file','engine_tool'}


def test_delivered_native_status_uses_canonical_intentional_silence_boundary():
    from agent.turn_tool_round import run_tool_round
    from gateway.response_filters import is_intentional_silence_agent_result
    agent = SimpleNamespace(quiet_mode=True, verbose_logging=False,
        _deduplicate_tool_calls=lambda x:x, _cap_delegate_task_calls=lambda x:x,
        _flush_messages_to_session_db=lambda *a: True, _emit_interim_assistant_message=Mock(),
        stream_delta_callback=None, _execute_tool_calls=Mock(), _incremental_persistence_failed=False,
        _tool_guardrail_halt_decision=None, _review_yield_requested=True, _review_status_delivered=True)
    with patch('agent.turn_tool_round.validate_tool_calls', return_value=SimpleNamespace(action='run',mixed_invalid_batch=False)), patch(
        'agent.turn_tool_round.stage_tool_call_message', return_value=({'role':'assistant','content':None},True)):
        verdict = run_tool_round(agent, assistant_message=SimpleNamespace(tool_calls=[]), finish_reason='tool_calls',
            messages=[], conversation_history=[], api_call_count=1, effective_task_id='parent', user_message='review',
            system_message='', active_system_prompt='', compression_attempts=0, max_compression_attempts=3,
            final_response='', failed=False, _turn_exit_reason=None, truncated_tool_call_retries=0)
    assert verdict.action == 'break' and verdict._turn_exit_reason == 'review_dispatched'
    assert verdict.final_response == '[SILENT]'
    assert is_intentional_silence_agent_result({'final_response':verdict.final_response}, verdict.final_response)
