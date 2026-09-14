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
            _should_sanitize_tool_calls=lambda: False, ephemeral_system_prompt=None,
            _current_turn_timestamp=10_000.0)
        for history in (messages, db.get_messages_as_conversation(agent.session_id)):
            wire, _ = build_api_messages(replay_agent, history, current_turn_user_idx=None,
                ext_prefetch_cache=None, plugin_user_context=None, moa_config=None, active_system_prompt=None)
            assert wire[-1]["content"] == "canonical"
            assert wire[0]["content"] == "earlier user context"
            assert wire[1]["content"] == "earlier wire"
    finally:
        db.close()


@pytest.mark.parametrize("followthrough", ["hidden", "summary", "steer", "summary_with_user"])
def test_finalization_collapses_only_current_human_turn_candidates(tmp_path, monkeypatch, followthrough):
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, SUMMARY_PREFIX, _SUMMARY_END_MARKER
    from agent.prompt_builder import STEER_DISPLAY_KIND

    boundary = {"role": "user", "content": "Deferred result follow-through", "display_kind": "hidden"}
    if followthrough in {"summary", "summary_with_user"}:
        boundary = {"role": "user", "content": f"{SUMMARY_PREFIX}\nMachine context", COMPRESSED_SUMMARY_METADATA_KEY: True}
        if followthrough == "summary_with_user":
            boundary["content"] += f"\n\n{_SUMMARY_END_MARKER}\n\nNew human instruction"
    elif followthrough == "steer":
        boundary = {"role": "user", "content": "New human instruction", "display_kind": STEER_DISPLAY_KIND}
    prior = {"role": "assistant", "content": "Prior turn answer", "_verification_candidate": True}
    provisional = {"role": "assistant", "content": "Current provisional", "_verification_candidate": True}
    messages = [
        {"role": "user", "content": "Prior task"}, prior,
        {"role": "user", "content": "Current task"}, provisional, boundary,
        {"role": "assistant", "content": "Latest provisional", "_verification_candidate": True},
    ]
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess-test", source="cli")
    agent = FakeAgent()
    agent._session_db = db
    db.replace_messages(agent.session_id, messages, active_only=True)
    monkeypatch.setattr("hermes_cli.lifecycle.transform_llm_output", lambda response, **kw: (response, False))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: [])
    try:
        finalize_turn(agent, final_response="Canonical answer", api_call_count=1, interrupted=False, failed=False,
                      messages=messages, conversation_history=[], effective_task_id="task", turn_id="turn",
                      user_message="Current task", original_user_message="Current task", _should_review_memory=False,
                      _turn_exit_reason="text_response")
        for history in (messages, db.get_messages_as_conversation(agent.session_id)):
            contents = [row.get("content") for row in history]
            assert "Prior turn answer" in contents
            assert ("Current provisional" in contents) == (followthrough in {"steer", "summary_with_user"})
            assert "Latest provisional" not in contents
            assert contents.count("Canonical answer") == 1
    finally:
        db.close()
