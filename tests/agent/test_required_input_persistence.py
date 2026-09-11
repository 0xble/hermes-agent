"""Required replay ingestion fails before both conversation runtime branches."""

from unittest.mock import MagicMock

import pytest

from agent import conversation_loop
from agent.turn_context import RequiredInputPersistenceError, _require_durable_input
from hermes_state import SessionDB
from tests.agent.test_turn_context import _FakeAgent, _build


@pytest.fixture
def agent(tmp_path):
    value = _FakeAgent()
    value._session_db = SessionDB(db_path=tmp_path / "state.db")
    value._session_db.create_session(value.session_id, source="telegram")
    yield value
    value._session_db.close()


META = {"gateway_input_owner": "stable-owner", "gateway_input_required": True}


@pytest.mark.parametrize("mode", ["codex_app_server", "chat_completions"])
@pytest.mark.parametrize("failure", ["swallowed", "false_flush", "missing_db", "read_failure"])
def test_real_conversation_entrypoint_blocks_dispatch(agent, monkeypatch, mode, failure):
    agent.api_mode = mode
    agent._try_refresh_env_client_credentials = lambda: None
    agent._run_codex_app_server_turn = MagicMock(side_effect=AssertionError("Codex dispatched"))
    request = MagicMock(side_effect=AssertionError("normal request loop entered"))
    monkeypatch.setattr(conversation_loop, "begin_iteration", request)
    monkeypatch.setattr(conversation_loop, "begin_fast_mode_turn", lambda *_a: None)
    # Keep the real conversation entrypoint and real turn prologue. Only external services are fakes.
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *_a, **_kw: None)
    if failure == "swallowed":
        agent._persist_session = MagicMock(side_effect=OSError("disk full"))
    elif failure == "false_flush":
        agent._persist_session = MagicMock(return_value=False)
    elif failure == "missing_db":
        agent._session_db.close()
        agent._session_db = None
    else:
        monkeypatch.setattr(agent._session_db, "has_gateway_input_owner", MagicMock(side_effect=OSError("read failed")))
    db = agent._session_db
    try:
        with pytest.raises(RequiredInputPersistenceError):
            conversation_loop.run_conversation(agent, "accepted input", persist_user_display_metadata=META)
        agent._run_codex_app_server_turn.assert_not_called()
        request.assert_not_called()
    finally:
        if db is None:
            # Fixture teardown owns the original close; keep a harmless close handle.
            agent._session_db = MagicMock()


def test_required_barrier_persists_canonical_input_once_before_compaction(agent, monkeypatch):
    from agent.session_persistence import SessionPersistenceMixin
    for name in ("_persist_session", "_drop_trailing_empty_response_scaffolding",
                 "_flush_messages_to_session_db", "_flush_messages_to_session_db_unlocked"):
        setattr(agent, name, getattr(SessionPersistenceMixin, name).__get__(agent))
    agent._session_db_created = True
    import agent.turn_context_compaction as compaction
    original = compaction.run_turn_start_compaction
    def before_compaction(*args, **kwargs):
        assert agent._session_db.has_gateway_input_owner(agent.session_id, META["gateway_input_owner"])
        return original(*args, **kwargs)
    monkeypatch.setattr(compaction, "run_turn_start_compaction", before_compaction)
    _build(agent, user_message="API enrichment", persist_user_message="canonical user input",
           persist_user_display_metadata=META)
    rows = agent._session_db.get_messages(agent.session_id)
    assert [row["content"] for row in rows if row["role"] == "user"] == ["canonical user input"]


def test_compression_ancestor_proof_avoids_a_second_input_write(agent):
    db = agent._session_db
    db.append_message(agent.session_id, "user", "accepted", display_metadata=META)
    db.end_session(agent.session_id, "compression")
    db.create_session("child", source="telegram", parent_session_id=agent.session_id)
    agent.session_id = "child"
    agent._persist_session = MagicMock(side_effect=AssertionError("duplicate write"))
    _require_durable_input(agent, META, [], None, None)
    _require_durable_input(agent, META, [], None, None)
    agent._persist_session.assert_not_called()


def test_ordinary_turn_keeps_best_effort_persistence(agent):
    agent._persist_session = MagicMock(side_effect=OSError("disk full"))
    result = _build(agent, persist_user_display_metadata={"gateway_input_owner": "ordinary"})
    assert result.user_message == "hello"


def test_goal_wrapper_does_not_convert_required_failure_to_completion():
    from gateway.run_turn_runner import TurnRunner
    runner = object.__new__(TurnRunner)
    runner._run_sync = MagicMock(side_effect=RequiredInputPersistenceError("disk"))
    runner._prepare_failed_goal_result = MagicMock()
    with pytest.raises(RequiredInputPersistenceError):
        runner.run_sync()
    runner._prepare_failed_goal_result.assert_not_called()
