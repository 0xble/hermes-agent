"""Canonical response rewrites must survive database reload and provider replay."""
from types import SimpleNamespace

import pytest

from agent.turn_context import build_api_messages
from agent.turn_finalizer import _collapse_verification_candidates, synchronize_terminal_response, finalize_turn
from hermes_state import SessionDB
from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent


@pytest.mark.parametrize("boundary", ["collapse", "terminal", "output"])
def test_canonical_rewrite_invalidates_only_its_replay_sidecar(tmp_path, monkeypatch, boundary):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-test", source="cli")
    agent = FakeAgent()
    agent._session_db = db
    messages = [
        {"role": "user", "content": "earlier", "api_content": "earlier user context"},
        {"role": "assistant", "content": "earlier answer", "api_content": "earlier wire"},
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "old", "api_content": "STALE WIRE"},
    ]
    try:
        if boundary == "collapse":
            messages[-1]["_verification_candidate"] = True
            assert _collapse_verification_candidates(messages, "canonical", agent)
            db.replace_messages(agent.session_id, messages, active_only=True)
        elif boundary == "terminal":
            synchronize_terminal_response(agent, {"messages": messages}, "canonical")
        else:
            monkeypatch.setattr("hermes_cli.lifecycle.transform_llm_output", lambda *a, **k: ("canonical", True))
            monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: [])
            finalize_turn(agent, final_response="old", api_call_count=1, interrupted=False, failed=False,
                messages=messages, conversation_history=[], effective_task_id="task", turn_id="turn",
                user_message="current", original_user_message="current", _should_review_memory=False,
                _turn_exit_reason="text_response")
        assert "api_content" not in messages[-1]
        assert messages[0]["api_content"] == "earlier user context"
        assert messages[1]["api_content"] == "earlier wire"
        replay_agent = SimpleNamespace(_copy_reasoning_content_for_api=lambda *a: None,
            _should_sanitize_tool_calls=lambda: False, ephemeral_system_prompt=None)
        for history in (messages, db.get_messages_as_conversation(agent.session_id)):
            wire, _ = build_api_messages(replay_agent, history, current_turn_user_idx=None,
                ext_prefetch_cache=None, plugin_user_context=None, moa_config=None, active_system_prompt=None)
            assert wire[-1]["content"] == "canonical"
            assert wire[0]["content"] == "earlier user context"
            assert wire[1]["content"] == "earlier wire"
    finally:
        db.close()
