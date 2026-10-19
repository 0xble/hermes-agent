"""Restart-safety regressions for proactive tool-result pruning."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.context_compressor import _estimate_msg_budget_tokens
from hermes_state import SessionDB

_REARM_KEY = "_proactive_prune_rearm_tokens"

def _assistant_call(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "terminal", "arguments": '{"cmd":"ls"}'},
        }],
    }

def _tool_result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}

def _history(*, large_chars: int = 24_000) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "start"}]
    for index in range(8):
        call_id = f"call_{index}"
        messages.append(_assistant_call(call_id))
        content = chr(65 + index) * large_chars if index < 3 else "ok"
        messages.append(_tool_result(call_id, content))
    return messages

def _build_agent(db: SessionDB, session_id: str, *, platform: str = "telegram"):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            platform=platform,
            skip_context_files=True,
            skip_memory=True,
        )

def _configure_pruning(agent) -> None:
    compressor = agent.context_compressor
    compressor.proactive_prune_tokens = 48_000
    compressor.proactive_prune_min_result_chars = 8_000
    compressor.proactive_prune_min_reclaim_tokens = 4_096
    compressor.protect_first_n = 2
    compressor.protect_last_n = 4

def _model_config(db: SessionDB, session_id: str) -> dict:
    raw = db.get_session(session_id)["model_config"]
    return json.loads(raw) if raw else {}

def test_gateway_eviction_reload_keeps_prune_and_durable_runway(tmp_path: Path) -> None:
    """A fresh gateway agent must reload both the pruned body and its runway."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "GATEWAY_PRUNE_RESTART"
    db.create_session(
        session_id, source="telegram", model_config={"keep": "value"},
    )
    db.append_messages_batch(session_id, _history())

    first_agent = _build_agent(db, session_id)
    _configure_pruning(first_agent)
    before = db.get_messages_as_conversation(session_id)
    pruned, count = first_agent.context_compressor.prune_tool_results_only(
        before, current_tokens=120_000,
    )

    assert count >= 1
    durable = db.get_messages_as_conversation(session_id)
    assert [message["content"] for message in durable] == [
        message["content"] for message in pruned
    ]
    assert len(durable[2]["content"]) < 24_000
    stored_runway = _model_config(db, session_id)[_REARM_KEY]
    assert _model_config(db, session_id)["keep"] == "value"
    assert stored_runway > sum(map(_estimate_msg_budget_tokens, durable))

    # Simulate gateway cache eviction / process restart: construct a wholly
    # new AIAgent and load the active transcript from SQLite.
    resumed_agent = _build_agent(db, session_id)
    _configure_pruning(resumed_agent)
    assert resumed_agent.context_compressor._proactive_prune_rearm_tokens == stored_runway
    reloaded = db.get_messages_as_conversation(session_id)
    archived_before = len(db.get_messages(session_id, include_inactive=True))
    result, second_count = resumed_agent.context_compressor.prune_tool_results_only(
        reloaded, current_tokens=1_000_000,
    )

    assert result is reloaded
    assert second_count == 0
    assert len(db.get_messages(session_id, include_inactive=True)) == archived_before

def test_fresh_agent_rearms_after_durable_history_regrowth_once(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PRUNE_DURABLE_REGROWTH"
    db.create_session(session_id, source="telegram")
    db.append_messages_batch(session_id, _history())
    first_agent = _build_agent(db, session_id)
    _configure_pruning(first_agent)
    _first, first_count = first_agent.context_compressor.prune_tool_results_only(
        db.get_messages_as_conversation(session_id), current_tokens=120_000,
    )
    assert first_count >= 1
    first_runway = _model_config(db, session_id)[_REARM_KEY]

    growth = [
        _assistant_call("regrown_large"),
        _tool_result("regrown_large", "z" * 240_000),
        _assistant_call("tail_1"),
        _tool_result("tail_1", "ok"),
        _assistant_call("tail_2"),
        _tool_result("tail_2", "ok"),
    ]
    db.append_messages_batch(session_id, growth)

    resumed = _build_agent(db, session_id)
    _configure_pruning(resumed)
    grown = db.get_messages_as_conversation(session_id)
    assert sum(map(_estimate_msg_budget_tokens, grown)) >= first_runway
    _second, second_count = resumed.context_compressor.prune_tool_results_only(
        grown, current_tokens=1_000_000,
    )

    assert second_count >= 1
    second_runway = _model_config(db, session_id)[_REARM_KEY]
    assert second_runway > first_runway

    restarted = _build_agent(db, session_id)
    _configure_pruning(restarted)
    durable = db.get_messages_as_conversation(session_id)
    result, third_count = restarted.context_compressor.prune_tool_results_only(
        durable, current_tokens=1_000_000,
    )
    assert result is durable
    assert third_count == 0
    assert restarted.context_compressor._proactive_prune_rearm_tokens == second_runway

def test_prune_persistence_failure_is_a_noop(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PRUNE_PERSISTENCE_FAILURE"
    db.create_session(session_id, source="telegram")
    db.append_messages_batch(session_id, _history())
    agent = _build_agent(db, session_id)
    _configure_pruning(agent)
    messages = db.get_messages_as_conversation(session_id)
    original_contents = [message["content"] for message in messages]

    with patch.object(
        db, "archive_and_compact", side_effect=RuntimeError("disk full"),
    ):
        result, count = agent.context_compressor.prune_tool_results_only(
            messages, current_tokens=120_000,
        )

    assert result is messages
    assert count == 0
    assert agent.context_compressor._proactive_prune_rearm_tokens == 0
    assert [message["content"] for message in messages] == original_contents
    assert [message["content"] for message in db.get_messages_as_conversation(session_id)] == original_contents
    assert _REARM_KEY not in _model_config(db, session_id)

def test_archive_model_config_patch_rolls_back_with_transcript(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PRUNE_ATOMIC_ARCHIVE_FAILURE"
    db.create_session(
        session_id,
        source="telegram",
        model_config={"keep": "value", _REARM_KEY: 120_000},
    )
    original = [{"role": "user", "content": "original"}]
    db.append_messages_batch(session_id, original)

    with patch.object(
        db, "_insert_message_rows", side_effect=RuntimeError("insert failed"),
    ):
        with pytest.raises(RuntimeError, match="insert failed"):
            db.archive_and_compact(
                session_id,
                [{"role": "user", "content": "replacement"}],
                model_config_patch={_REARM_KEY: None},
            )

    assert db.get_messages_as_conversation(session_id)[0]["content"] == "original"
    assert _model_config(db, session_id) == {"keep": "value", _REARM_KEY: 120_000}

def test_model_switch_clears_durable_runway(tmp_path: Path) -> None:
    """update_model must clear BOTH the in-memory and the durable runway."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "MODEL_SWITCH_CLEARS_RUNWAY"
    db.create_session(
        session_id,
        source="telegram",
        model_config={"keep": "value", _REARM_KEY: 120_000},
    )
    agent = _build_agent(db, session_id)
    compressor = agent.context_compressor
    assert compressor._proactive_prune_rearm_tokens == 120_000

    compressor.update_model("other/model", 200_000)

    assert compressor._proactive_prune_rearm_tokens == 0
    assert _REARM_KEY not in _model_config(db, session_id)
    assert _model_config(db, session_id)["keep"] == "value"

def test_patch_session_model_config_merge_and_delete(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PATCH_MODEL_CONFIG"
    db.create_session(
        session_id, source="cli", model_config={"keep": "value", "drop": 1},
    )

    db.patch_session_model_config(session_id, {"drop": None, "added": 7})
    assert _model_config(db, session_id) == {"keep": "value", "added": 7}

    # Missing rows and empty patches are no-ops, never errors.
    db.patch_session_model_config("NO_SUCH_SESSION", {"x": 1})
    db.patch_session_model_config(session_id, {})

def _compacted_session(db: SessionDB, session_id: str) -> None:
    """A session after one summary-producing compaction, then fresh large tool results."""
    db.create_session(session_id, source="telegram")
    db.append_messages_batch(session_id, _history())
    current = db.get_messages_as_conversation(session_id)
    db.archive_and_compact(
        session_id,
        [{"role": "user", "content": "[CONTEXT COMPACTION] summary", "_compressed_summary": True}, *current[-4:]],
        tail_count=4,
    )
    db.append_messages_batch(session_id, _history()[1:])

def _gateway_replay(db: SessionDB, session_id: str) -> list[dict]:
    from gateway.run import _build_gateway_agent_history

    history, _observed = _build_gateway_agent_history(
        db.get_messages_as_conversation(session_id, repair_alternation=True))
    return history

def test_gateway_replay_after_compaction_still_prunes(tmp_path: Path) -> None:
    """Compress once, then a gateway turn's lease-less prune must commit.

    The replay must keep each row's CAS snapshot: without it the held summary looks unheld and
    every prune is refused as "another compaction already committed this session"."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PRUNE_AFTER_COMPACTION"
    _compacted_session(db, session_id)
    agent = _build_agent(db, session_id)
    _configure_pruning(agent)

    _pruned, count = agent.context_compressor.prune_tool_results_only(
        _gateway_replay(db, session_id), current_tokens=120_000,
    )

    assert count >= 1
    durable = db.get_messages_as_conversation(session_id)
    assert any(m.get("content") == "[CONTEXT COMPACTION] summary" for m in durable)
    assert max(len(str(m.get("content") or "")) for m in durable) < 24_000

def test_gateway_replay_prune_still_aborts_after_a_newer_compaction(tmp_path: Path) -> None:
    """A compaction that commits after the replay was built must still win over the prune."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PRUNE_LOSES_TO_NEWER_COMPACTION"
    _compacted_session(db, session_id)
    agent = _build_agent(db, session_id)
    _configure_pruning(agent)
    held = _gateway_replay(db, session_id)

    current = db.get_messages_as_conversation(session_id)
    db.archive_and_compact(
        session_id,
        [{"role": "user", "content": "[CONTEXT COMPACTION] summary", "_compressed_summary": True}, *current[-4:]],
        tail_count=4,
    )
    winner = [m["content"] for m in db.get_messages_as_conversation(session_id)]

    result, count = agent.context_compressor.prune_tool_results_only(held, current_tokens=120_000)

    assert result is held
    assert count == 0
    assert [m["content"] for m in db.get_messages_as_conversation(session_id)] == winner

def test_gateway_replay_after_compaction_still_commits_micro_compaction(tmp_path: Path) -> None:
    """Micro-compaction shares the lease-less summary guard and must also commit after a compaction."""
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "MICRO_AFTER_COMPACTION"
    _compacted_session(db, session_id)
    agent = _build_agent(db, session_id)
    held = [dict(m) for m in _gateway_replay(db, session_id)]

    committed = agent.context_compressor._sync_micro_compact_to_db(
        [dict(m) for m in held], held=held, start_watermark=db.get_active_message_watermark(session_id),
    )

    assert committed is True
