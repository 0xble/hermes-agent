"""Tests for ``gateway.run._build_replay_entry``.

The gateway rebuilds ``agent_history`` from the persisted transcript on every
turn (unlike the CLI, which keeps the live in-memory message list).  When a
pure-text assistant turn (no ``tool_calls``) is replayed, the simple-text
branch in ``run_sync`` used to whitelist only three reasoning fields:
``reasoning``, ``reasoning_details``, ``codex_reasoning_items``.

That whitelist predated three fields the DB now persists:
``reasoning_content``, ``codex_message_items``, and ``finish_reason``.  The
unrecovered drop of ``codex_message_items`` in particular kills prefix-cache
hits for OpenAI Codex Responses API users — OpenAI's docs require the
``phase`` field be replayed on every assistant message.

These tests pin the expanded whitelist so it doesn't regress.
"""
from __future__ import annotations


from gateway.run import _build_replay_entry


class TestBuildReplayEntry:
    def test_user_message_has_only_role_and_content(self):
        entry = _build_replay_entry(
            "user",
            "hello",
            {"role": "user", "content": "hello", "reasoning": "leak", "extra": "drop"},
        )
        assert entry == {"role": "user", "content": "hello"}

    def test_tool_message_has_only_role_and_content(self):
        # Tool messages aren't routed through this helper in production
        # (they take the rich-passthrough branch), but the helper itself
        # must not leak reasoning fields onto non-assistant roles even if
        # someone calls it incorrectly.
        entry = _build_replay_entry(
            "tool",
            "result",
            {"role": "tool", "content": "result", "reasoning": "leak"},
        )
        assert entry == {"role": "tool", "content": "result"}


    def test_assistant_drops_falsy_reasoning(self):
        """Empty/None reasoning fields stay dropped (matching PR #2974
        behaviour) — empty strings/lists for these fields carry no info."""
        msg = {
            "role": "assistant",
            "content": "answer",
            "reasoning": "",
            "reasoning_details": [],
            "codex_reasoning_items": [],
            "codex_message_items": [],
            "finish_reason": "",
        }
        entry = _build_replay_entry("assistant", "answer", msg)
        assert entry == {"role": "assistant", "content": "answer"}


    def test_assistant_preserves_all_six_fields_together(self):
        details = [{"type": "reasoning.summary", "summary": "s"}]
        codex_items = [{"type": "reasoning", "encrypted_content": "b"}]
        msg_items = [
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "x"}],
            }
        ]
        msg = {
            "role": "assistant",
            "content": "answer",
            "reasoning": "thinking",
            "reasoning_content": "structured",
            "reasoning_details": details,
            "codex_reasoning_items": codex_items,
            "codex_message_items": msg_items,
            "finish_reason": "stop",
        }
        entry = _build_replay_entry("assistant", "answer", msg)
        assert entry["reasoning"] == "thinking"
        assert entry["reasoning_content"] == "structured"
        assert entry["reasoning_details"] == details
        assert entry["codex_reasoning_items"] == codex_items
        assert entry["codex_message_items"] == msg_items
        assert entry["finish_reason"] == "stop"



    def test_unrelated_keys_are_ignored(self):
        """Random keys on the message must not leak into the replay entry."""
        msg = {
            "role": "assistant",
            "content": "answer",
            "timestamp": 12345.6,
            "internal_marker": "should not flow",
            "tool_call_id": "should not be set on simple-text branch",
        }
        entry = _build_replay_entry("assistant", "answer", msg)
        assert "timestamp" not in entry
        assert "internal_marker" not in entry
        assert "tool_call_id" not in entry


class TestReplayEntryApiContentSidecar:
    """The api_content sidecar (persist-what-you-send) must survive the
    gateway's transcript→agent_history rebuild, or the whole prompt-cache
    fix is inert on gateway platforms — but only when this pipeline did not
    rewrite the content (a rewrite means different bytes must replay)."""

    def test_user_forwards_api_content_when_content_unchanged(self):
        msg = {"role": "user", "content": "hi", "api_content": "hi\n\nCTX"}
        entry = _build_replay_entry("user", "hi", msg)
        assert entry["api_content"] == "hi\n\nCTX"
        assert entry["content"] == "hi"

    def test_assistant_forwards_api_content_when_content_unchanged(self):
        msg = {"role": "assistant", "content": "a", "api_content": "a <memory-context>"}
        entry = _build_replay_entry("assistant", "a", msg)
        assert entry["api_content"] == "a <memory-context>"


class TestGatewayHistoryBuildForwardsSidecar:
    def test_end_to_end_history_build_keeps_sidecar(self):
        from gateway.run import _build_gateway_agent_history

        history = [
            {"role": "user", "content": "hi", "api_content": "hi\n\nCTX", "timestamp": 123.0},
            {"role": "assistant", "content": "hello"},
        ]
        agent_history, _obs = _build_gateway_agent_history(history)
        assert agent_history[0]["api_content"] == "hi\n\nCTX"



def test_gateway_history_keeps_sidecar_only_assistant_row():
    """A reasoning-only clean stop persists content="" with the promoted reply in ``api_content``
    (agent/turn_final_response.py); the gateway rebuild must replay it, not drop it (user->user)."""
    from gateway.run import _build_gateway_agent_history

    history = [
        {"role": "user", "content": "2+2?"},
        {"role": "assistant", "content": "", "reasoning": "The answer is 4.", "api_content": "The answer is 4."},
        {"role": "user", "content": "thanks"},
    ]
    agent_history, _ = _build_gateway_agent_history(history)
    assert [m["role"] for m in agent_history] == ["user", "assistant", "user"]
    assert agent_history[1]["api_content"] == "The answer is 4."
    assert agent_history[1]["reasoning"] == "The answer is 4."


def test_replay_rebuilds_the_same_conversation_so_every_role_keeps_its_identity():
    """Gateway replay turns the stored transcript back into the SAME session's history for its next turn:
    user, assistant, tool-calling and tool rows all keep ``message_uid`` and the merge witness."""
    from gateway.run import _build_gateway_agent_history

    call = {"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    history = [
        {"role": "user", "content": "a\n\nb", "message_uid": "1" * 32, "_absorbed_message_uids": ["b" * 32],
         "timestamp": 1_700_000_000.0},
        {"role": "assistant", "content": "", "tool_calls": [call], "message_uid": "2" * 32},
        {"role": "tool", "content": "r", "tool_call_id": "call_1", "message_uid": "3" * 32},
        {"role": "assistant", "content": "done", "message_uid": "4" * 32, "_absorbed_message_uids": ["c" * 32]},
    ]
    replayed, _ = _build_gateway_agent_history(history)
    assert [(m["role"], m.get("message_uid"), m.get("_absorbed_message_uids")) for m in replayed] == [
        (m["role"], m["message_uid"], m.get("_absorbed_message_uids")) for m in history]


def test_a_plain_replay_row_does_not_carry_a_tool_call_uid_map_without_its_calls():
    """Repair can prune an assistant's unanswered tool_calls and leave its ``_tool_call_uids`` behind: the
    replayed plain row keeps its own uid but not a map naming calls it no longer carries."""
    from gateway.run import _build_gateway_agent_history

    history = [
        {"role": "user", "content": "q", "message_uid": "1" * 32},
        {"role": "assistant", "content": "done", "message_uid": "2" * 32, "_tool_call_uids": {"call_1": "5" * 32}},
    ]
    replayed, _ = _build_gateway_agent_history(history)
    assert replayed[1]["message_uid"] == "2" * 32
    assert "_tool_call_uids" not in replayed[1] and "_tool_call_uid" not in replayed[1]


def test_replay_preserves_summary_metadata_and_does_not_timestamp_summary_rows():
    from gateway.run import _build_gateway_agent_history

    stored = [{
        "role": "user", "content": "[CONTEXT COMPACTION] summary", "message_uid": "u1",
        "_db_row_snapshot": "snap1", "_db_persisted": True, "_compressed_summary": True,
        "timestamp": 1_700_000_000.0, "message_id": "m1", "display_kind": "summary",
        "display_metadata": {"source": "compression"},
    }]
    replayed, _ = _build_gateway_agent_history(stored, inject_timestamps=True)
    entry = replayed[0]
    assert entry["content"] == stored[0]["content"]
    assert entry["_compressed_summary"] is True
    assert entry["_db_row_snapshot"] == "snap1"
    assert entry["message_id"] == "m1"
    assert entry["display_kind"] == "summary"
    assert entry["display_metadata"] == {"source": "compression"}



def test_replay_drops_persistence_metadata_when_content_is_rewritten():
    from gateway.run import _build_gateway_agent_history

    stored = [{
        "role": "user", "content": "hello", "message_uid": "u1", "_db_row_snapshot": "snap1",
        "_db_persisted": True, "_compressed_summary": True, "message_id": "m1", "observed": True,
        "display_kind": "summary", "display_metadata": {"source": "compression"}, "mirror": True,
        "mirror_source": "other",
    }]
    replayed, _ = _build_gateway_agent_history(stored)
    entry = replayed[0]
    assert entry["content"].startswith("[Delivered from other]")
    assert entry["_db_persisted"] is True
    assert all(key not in entry for key in (
        "_db_row_snapshot", "_compressed_summary", "message_id", "observed",
        "platform_message_id", "display_kind", "display_metadata", "effect_disposition",
    ))



def test_timestamp_replay_persists_original_content_and_row_metadata(tmp_path):
    from agent.message_metadata import REPLAY_PERSISTED_CONTENT
    from hermes_state import SessionDB
    from gateway.run import _build_gateway_agent_history

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", "gateway")
        stored = [{
            "role": "user", "content": "follow up", "message_uid": "u1", "_db_row_snapshot": "snap1",
            "_db_persisted": True, "platform_message_id": "plat-9", "display_kind": "user_text",
            "timestamp": 1_700_000_000.0,
        }]
        replayed, _ = _build_gateway_agent_history(stored, inject_timestamps=True)
        assert replayed[0]["content"] != "follow up"
        assert replayed[0][REPLAY_PERSISTED_CONTENT] == "follow up"
        assert replayed[0]["platform_message_id"] == "plat-9"
        assert replayed[0]["display_kind"] == "user_text"
        assert replayed[0]["_db_persisted"] is True
        from agent.session_persistence import _db_flush_row
        flush_row = _db_flush_row(object(), replayed[0], False)
        assert flush_row["content"] == "follow up"
        db.append_messages_batch("s", replayed)
        persisted = db.get_messages_as_conversation("s")[0]
        assert persisted["content"] == "follow up"
        assert persisted["message_id"] == "plat-9"
        assert persisted["display_kind"] == "user_text"
    finally:
        db.close()



def test_timestamped_mirror_replay_drops_view_identity_but_keeps_persisted_content():
    from agent.message_metadata import REPLAY_PERSISTED_CONTENT
    from gateway.run import _build_gateway_agent_history

    stored = [{
        "role": "user", "content": "hello", "message_uid": "u1", "_db_row_snapshot": "snap1",
        "_db_persisted": True, "message_id": "m1", "display_kind": "user_text",
        "mirror": True, "mirror_source": "other", "timestamp": 1_700_000_000.0,
    }]
    replayed, _ = _build_gateway_agent_history(stored, inject_timestamps=True)
    entry = replayed[0]
    assert entry[REPLAY_PERSISTED_CONTENT] == "hello"
    assert "_db_row_snapshot" not in entry
    assert "message_id" not in entry
    assert "display_kind" not in entry


def test_rewritten_replay_rows_stay_out_of_the_real_flush_path(tmp_path):
    from agent.session_persistence import SessionPersistenceMixin
    from gateway.run import _build_gateway_agent_history
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", "gateway")
        db.append_message("s", "user", content="hello")
        stored = [{
            "role": "user", "content": "hello", "_db_persisted": True,
            "_db_row_snapshot": "snap1", "message_uid": "u1", "mirror": True,
            "mirror_source": "other",
        }]
        replayed, _ = _build_gateway_agent_history(stored)
        assert replayed[0]["_db_persisted"] is True
        assert "_db_row_snapshot" not in replayed[0]

        agent = object.__new__(SessionPersistenceMixin)
        agent._session_db = db
        agent.session_id = "s"
        agent._session_db_created = True
        agent._flushed_db_message_ids = set()
        agent._last_flushed_db_idx = 0
        agent._db_flush_scan_prefix = None
        agent._persist_user_message_idx = None
        agent._pending_cli_user_message = None
        flushed = SessionPersistenceMixin._flush_messages_to_session_db.__get__(agent)
        assert flushed(replayed) is True
        assert [row["content"] for row in db.get_messages_as_conversation("s")] == ["hello"]
    finally:
        db.close()


def test_replay_keeps_the_stored_row_version_only_for_unchanged_content():
    from gateway.run import _build_gateway_agent_history

    stored = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary", "message_uid": "u1",
         "_db_row_snapshot": "snap1", "_db_persisted": True, "timestamp": 1_700_000_000.0},
        {"role": "assistant", "content": "ok", "message_uid": "u2", "_db_row_snapshot": "snap2",
         "_db_persisted": True},
    ]
    plain, _ = _build_gateway_agent_history(stored)
    assert [m.get("_db_row_snapshot") for m in plain] == ["snap1", "snap2"]

    rendered, _ = _build_gateway_agent_history(stored, inject_timestamps=True)
    assert rendered[0]["content"] != stored[0]["content"]
    assert rendered[0]["_replay_persisted_content"] == stored[0]["content"]
    assert rendered[0]["_db_row_snapshot"] == "snap1"
    assert rendered[1]["_db_row_snapshot"] == "snap2"
