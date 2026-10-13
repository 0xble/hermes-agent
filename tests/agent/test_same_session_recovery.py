"""Lossless recovery of a hard-window overflow, using native SessionDB boundaries."""
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from tests.agent.test_in_place_compaction import _make_agent


def _agent(db, sid="recovery"):
    db.create_session(sid, "telegram", model="test/model")
    db.set_session_title(sid, "Runtime")
    db.set_session_title_source(sid, "user")
    agent = _make_agent(db, sid, in_place=True)
    agent._cached_system_prompt = "Keep user directions."
    agent._compression_feasibility_checked = True
    agent.tools = []
    agent.max_tokens = 1024
    agent.context_compressor.max_tokens = 1024
    agent.context_compressor.context_length = 272000
    agent.context_compressor.compress = lambda messages, **kw: messages
    agent._build_system_prompt = lambda *a, **kw: "Keep user directions."
    agent._invalidate_system_prompt = lambda: None
    return agent


def _seed_tool(db, sid):
    rows = [
        {"role": "user", "content": "Do not repeat completed writes. Objective: finish task abc. Stop budget: 7."},
        {"role": "assistant", "content": "Completed write receipt: effect-17.", "tool_calls": [
            {"id": "read-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"artifact"}'}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": "archive-sentinel " + "x" * 1450000},
        {"role": "user", "content": "Continue task abc; no additional writes. New instruction: inspect only."},
    ]
    db.replace_messages(sid, rows)
    return db.get_messages_as_conversation(sid)


def test_reported_shape_no_progress_recovers_with_archive_and_usable_request(tmp_path):
    from agent.model_metadata import estimate_request_tokens_rough

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    messages = _seed_tool(db, agent.session_id)
    originals = {r["id"]: r["content"] for r in db.get_messages(agent.session_id)}
    with patch("agent.conversation_compression._refresh_agent_tool_definitions"):
        result, prompt = agent._compress_context(messages, "", approx_tokens=352895)
    assert estimate_request_tokens_rough(result, system_prompt=prompt, tools=agent.tools) + 1024 < 272000
    assert agent.session_id == "recovery"
    assert db.get_session_title("recovery") == "Runtime"
    assert db.get_session_title_source("recovery") == "user"
    assert result[-1]["content"] == messages[-1]["content"]
    assert result[0]["content"] == messages[0]["content"]
    assert result[1]["tool_calls"] == messages[1]["tool_calls"]
    assert "session_search" in result[2]["content"]
    db.close()
    restarted = SessionDB(db_path=tmp_path / "state.db")
    retained = {r["id"]: r["content"] for r in restarted.get_messages("recovery", include_inactive=True)}
    assert all(retained[rid] == content for rid, content in originals.items())
    active = restarted.get_messages_as_conversation("recovery")
    assert estimate_request_tokens_rough(active, system_prompt=prompt) + 1024 < 272000
    assert any("archive-sentinel" in r["content"] for r in restarted.get_messages("recovery", include_inactive=True))


def test_triggering_input_is_durable_before_preflight_failure(tmp_path):
    from agent.turn_context import PreflightCompressionTimedOut

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    agent.client = MagicMock()
    agent.save_trajectories = False
    agent._use_prompt_caching = False
    # A turn that may compact (P3 scoping): only such turns can hit this early exit.
    agent.context_compressor.threshold_tokens = 1
    with patch("agent.turn_context_compaction.run_turn_start_compaction", side_effect=PreflightCompressionTimedOut("provider call was not sent")):
        result = agent.run_conversation("inspect task abc", persist_user_platform_id="telegram:101")
    assert result["failed"] is True
    agent.client.chat.completions.create.assert_not_called()
    db.close()
    restarted = SessionDB(db_path=tmp_path / "state.db")
    rows = restarted.get_messages("recovery", include_inactive=True)
    assert [r["content"] for r in rows if r["role"] == "user"] == ["inspect task abc"]
    assert restarted.has_platform_message_id("recovery", "telegram:101")


def test_unfittable_newest_user_pauses_without_clipping_or_archiving(tmp_path):
    from agent.compression_recovery import try_hard_window_recovery

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    rows[-1]["content"] = "MANDATORY INSTRUCTION " * 100000
    db.replace_messages(agent.session_id, rows)
    rows = db.get_messages_as_conversation(agent.session_id)
    original_ids = [r["id"] for r in db.get_messages(agent.session_id)]
    result, _ = try_hard_window_recovery(agent, rows, "", agent._cached_system_prompt)
    assert result is rows
    assert [r["id"] for r in db.get_messages(agent.session_id)] == original_ids
    assert result[-1]["content"] == rows[-1]["content"]


def test_explicit_stop_refuses_recovery(tmp_path):
    from agent.compression_recovery import try_hard_window_recovery

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    agent._interrupt_requested = True
    result, _ = try_hard_window_recovery(agent, rows, "", agent._cached_system_prompt)
    assert result is rows
    assert len(db.get_messages(agent.session_id)) == 4


def test_atomic_archive_failure_returns_intact_input(tmp_path):
    from agent.compression_recovery import try_hard_window_recovery

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    before = db.get_messages(agent.session_id)
    with patch.object(db, "archive_and_compact", side_effect=RuntimeError("injected commit failure")) as commit:
        result, _ = try_hard_window_recovery(agent, rows, "", agent._cached_system_prompt)
    commit.assert_called_once()
    assert result == rows
    assert db.get_messages(agent.session_id) == before


def test_newer_pending_arrival_survives_recovery_watermark(tmp_path):
    from agent.compression_recovery import try_hard_window_recovery

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    db.append_message(agent.session_id, "user", "Queued after snapshot: stop after inspecting.", platform_message_id="telegram:102")
    with patch("agent.conversation_compression._refresh_agent_tool_definitions"):
        recovered, _ = try_hard_window_recovery(agent, rows, "", agent._cached_system_prompt)
    active = db.get_messages_as_conversation(agent.session_id)
    assert active[-1]["content"] == "Queued after snapshot: stop after inspecting."
    assert db.has_platform_message_id(agent.session_id, "telegram:102")
    assert sum(r["content"] == "Queued after snapshot: stop after inspecting." for r in active) == 1
    assert any("session_search" in r.get("content", "") for r in recovered)


def test_active_tool_group_reasoning_and_call_arguments_remain_verbatim(tmp_path):
    from agent.compression_recovery import recovery_projection

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    rows.pop()  # latest user is the still-running opener, before its tool chain
    rows[1]["codex_reasoning_items"] = [{"type": "reasoning", "id": "reasoning-1", "encrypted_content": "opaque"}]
    result = recovery_projection(agent, rows)
    assert result[1]["codex_reasoning_items"] == rows[1]["codex_reasoning_items"]
    assert result[1]["tool_calls"] == rows[1]["tool_calls"]
    assert result[2]["tool_call_id"] == "read-1"
    assert rows[2]["content"].endswith("x" * 10000)


def test_system_tools_output_and_safety_reserve_can_make_projection_unfittable(tmp_path):
    from agent.compression_recovery import recovery_projection

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    agent.ephemeral_system_prompt = "mandatory system instructions " * 50000
    assert recovery_projection(agent, rows) is rows


@pytest.mark.parametrize("reason", ["cooldown:summary_failure", "structural_backoff:recent"])
def test_transient_cooldown_pauses_without_mechanical_commit(tmp_path, reason):
    from agent.compression_recovery import try_hard_window_recovery
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    agent._compression_blocked_transient = reason
    before = db.get_messages(agent.session_id, include_inactive=True)
    out, _ = try_hard_window_recovery(agent, rows, "", "")
    assert out is rows
    assert db.get_messages(agent.session_id, include_inactive=True) == before


def test_unknown_window_never_authorizes_archive(tmp_path):
    from agent.compression_recovery import request_fits, try_hard_window_recovery
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    from types import SimpleNamespace
    agent.context_compressor = SimpleNamespace(context_length=None)
    assert request_fits(agent, rows, "") is False
    out, _ = try_hard_window_recovery(agent, rows, "", "")
    assert out is rows
    assert not any(row["compacted"] for row in db.get_messages(agent.session_id, include_inactive=True))


def test_admission_checkpoint_failure_does_not_send(tmp_path):
    from agent.compression_recovery import admit_before_compaction
    from agent.turn_context import PreflightCompressionTimedOut
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    # Over the preflight threshold: this turn may compact, so admission must fail closed.
    rows = [{"role": "user", "content": "inspect only " + "x" * 4_000_000}]
    with patch.object(agent, "_flush_messages_to_session_db", return_value=False), pytest.raises(
            PreflightCompressionTimedOut, match="checkpoint"):
        admit_before_compaction(agent, rows, [], rows[0])
    assert agent._session_messages is rows


def test_required_memory_checkpoint_blocks_mechanical_archive(tmp_path):
    from agent.conversation_compression import CompressionCheckpointUnavailable
    from agent.compression_recovery import try_hard_window_recovery
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    before = db.get_messages(agent.session_id, include_inactive=True)
    with patch("agent.conversation_compression._pre_compress_memory_context",
               side_effect=CompressionCheckpointUnavailable("durable checkpoint unavailable")), pytest.raises(
                   CompressionCheckpointUnavailable):
        try_hard_window_recovery(agent, rows, "", "")
    assert db.get_messages(agent.session_id, include_inactive=True) == before


def test_complete_request_includes_media_and_output_runway(tmp_path):
    from agent.compression_recovery import request_fits
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    agent.context_compressor.context_length = 8000
    rows = [{"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "https://example.test/image.png"}}, {"type": "text", "text": "inspect"}]}]
    assert request_fits(agent, rows, "")
    agent._ephemeral_max_output_tokens = 7500
    assert not request_fits(agent, rows, "")


def test_projection_survives_api_sidecar_replay(tmp_path):
    from agent.compression_recovery import recovery_projection
    from agent.turn_context import build_api_messages
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    rows[2]["api_content"] = rows[2]["content"]
    projected = recovery_projection(agent, rows)
    assert projected is not rows
    agent._current_turn_timestamp = 1700000000.0
    api, _ = build_api_messages(agent, projected, current_turn_user_idx=3,
        ext_prefetch_cache="", plugin_user_context="", moa_config=None, active_system_prompt="")
    assert len(next(m for m in api if m["role"] == "tool")["content"]) < 10000
