"""Durable side-to-main context merge regressions."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.context_compressor import SUMMARY_PREFIX
from gateway.platforms.base import MessageEvent, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from hermes_state import SessionDB


def _seed_side(db: SessionDB):
    db.create_session("main", source="telegram")
    db.append_message("main", role="user", content="main question", timestamp=1.0)
    db.append_message("main", role="assistant", content="main answer", timestamp=2.0)
    db.create_session(
        "side",
        source="telegram",
        parent_session_id="main",
        model_config={
            "_branched_from": "main",
            "_side_from": "main",
            "_side_root": "side",
            "_side_parent_route": "agent:default:telegram:dm:chat",
            "_side_fork_message_count": 2,
        },
    )
    db.append_messages_batch(
        "side",
        db.get_messages_as_conversation("main"),
    )
    db.append_message("side", role="user", content="side question", timestamp=3.0)
    db.append_message(
        "side",
        role="assistant",
        content="side answer",
        finish_reason="stop",
        timestamp=4.0,
    )


def test_merge_imports_only_side_delta_and_is_idempotent(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)

    first = db.merge_side_context(
        destination_session_id="main",
        side_root_session_id="side",
    )
    duplicate = db.merge_side_context(
        destination_session_id="main",
        side_root_session_id="side",
    )

    assert first["status"] == "merged"
    assert first["imported_messages"] == 2
    assert duplicate["status"] == "already_merged"
    history = db.get_messages_as_conversation("main")
    assert [message["role"] for message in history] == [
        "user", "assistant", "user", "assistant"
    ]
    receipt = history[-1]
    assert receipt["display_kind"] == "session_merge"
    packet_text = receipt["api_content"]
    assert "side question" in packet_text
    assert "side answer" in packet_text
    assert packet_text.count("main question") == 0
    assert "does not authorize repeating" in packet_text


def test_merge_reapplies_a_receipt_rewound_out_of_main(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    first = db.merge_side_context(
        destination_session_id="main", side_root_session_id="side"
    )
    command = db.get_messages_as_conversation("main", include_row_ids=True)[-2]
    db.rewind_to_message("main", int(command["_row_id"]))

    reapplied = db.merge_side_context(
        destination_session_id="main", side_root_session_id="side"
    )

    assert reapplied["status"] == "merged"
    assert reapplied["receipt_message_id"] != first["receipt_message_id"]
    history = db.get_messages_as_conversation("main")
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    with db._read_ctx() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count, receipt_message_id FROM session_context_merges"
        ).fetchone()
    assert int(row["count"]) == 1
    assert int(row["receipt_message_id"]) == reapplied["receipt_message_id"]


def test_second_merge_imports_only_new_side_messages(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    db.merge_side_context(destination_session_id="main", side_root_session_id="side")
    db.append_message("side", role="user", content="later question", timestamp=5.0)
    db.append_message(
        "side", role="assistant", content="later answer", finish_reason="stop", timestamp=6.0
    )

    result = db.merge_side_context(
        destination_session_id="main",
        side_root_session_id="side",
        command_text="/fold",
    )

    assert result["status"] == "merged"
    assert result["imported_messages"] == 2
    receipt = db.get_messages_as_conversation("main")[-1]
    packet = json.loads(
        receipt["api_content"].split("<side-session-snapshot>\n", 1)[1].split(
            "\n</side-session-snapshot>", 1
        )[0]
    )
    assert [message["content"] for message in packet["messages"]] == [
        "later question",
        "later answer",
    ]
    assert db.get_messages_as_conversation("side")[-1]["content"] == "later answer"


def test_merge_fails_closed_after_main_reset(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    db.create_session(
        "reset-main",
        source="telegram",
        parent_session_id="main",
        model_config={"_reset_from": "main"},
    )

    try:
        db.merge_side_context(
            destination_session_id="reset-main",
            side_root_session_id="side",
        )
    except ValueError as exc:
        assert "reset or replaced" in str(exc)
    else:
        raise AssertionError("reset destination must fail closed")


@pytest.mark.parametrize("record_fork_count", [True, False])
@pytest.mark.parametrize(
    "side_question",
    ["side q", f"{SUMMARY_PREFIX}\nuser-authored lookalike"],
)
def test_merge_does_not_resurrect_parent_rows_rewound_after_fork(
    tmp_path, record_fork_count, side_question
):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("main", source="telegram")
    for role, content, timestamp in (
        ("user", "q1", 1.0),
        ("assistant", "a1", 2.0),
        ("user", "q2", 3.0),
        ("assistant", "a2", 4.0),
    ):
        db.append_message("main", role=role, content=content, timestamp=timestamp)
    parent_history = db.get_messages_as_conversation("main", include_row_ids=True)
    side_config: dict[str, object] = {
        "_branched_from": "main",
        "_side_from": "main",
        "_side_root": "side",
    }
    if record_fork_count:
        side_config["_side_fork_message_count"] = len(parent_history)
    db.create_session(
        "side",
        source="telegram",
        parent_session_id="main",
        model_config=side_config,
    )
    q2_row_id = next(
        message["_row_id"] for message in parent_history if message["content"] == "q2"
    )
    db.append_messages_batch("side", parent_history)
    db.append_message("side", role="user", content=side_question, timestamp=5.0)
    db.append_message("side", role="assistant", content="side a", timestamp=6.0)
    db.rewind_to_message("main", q2_row_id)

    result = db.merge_side_context(
        destination_session_id="main",
        side_root_session_id="side",
    )

    assert result["imported_messages"] == 2
    receipt = db.get_messages_as_conversation("main")[-1]
    assert side_question.splitlines()[-1] in receipt["api_content"]
    assert '"content":"q2"' not in receipt["api_content"]
    assert '"content":"a2"' not in receipt["api_content"]


def test_main_compaction_does_not_turn_the_side_seed_into_merge_delta(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    db.archive_and_compact(
        "main",
        [
            {"role": "user", "content": f"{SUMMARY_PREFIX}\nmain summary"},
            {"role": "assistant", "content": "main answer", "finish_reason": "stop"},
        ],
        watermark=db.get_active_message_watermark("main"),
        tail_count=1,
    )

    result = db.merge_side_context(
        destination_session_id="main",
        side_root_session_id="side",
    )

    assert result["imported_messages"] == 2
    receipt = db.get_messages_as_conversation("main")[-1]
    assert "side question" in receipt["api_content"]
    assert "side answer" in receipt["api_content"]
    assert "main question" not in receipt["api_content"]


def test_merge_imports_a_compacted_side_checkpoint_and_then_new_delta(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("main", source="telegram")
    for role, content, timestamp in (
        ("user", "parent q", 1.0),
        ("assistant", "parent a", 2.0),
    ):
        db.append_message("main", role=role, content=content, timestamp=timestamp)
    parent_history = db.get_messages_as_conversation("main")
    db.create_session(
        "side",
        source="telegram",
        parent_session_id="main",
        model_config={
            "_branched_from": "main",
            "_side_from": "main",
            "_side_root": "side",
            "_side_fork_message_count": len(parent_history),
        },
    )
    db.append_messages_batch("side", parent_history)
    db.append_message("side", role="user", content="side q", timestamp=3.0)
    db.append_message("side", role="assistant", content="side a", timestamp=4.0)

    watermark = db.get_active_message_watermark("side")
    db.archive_and_compact(
        "side",
        [
            {
                "role": "user",
                "content": f"{SUMMARY_PREFIX}\nparent q/parent a; side q/side a",
            },
            {"role": "assistant", "content": "side a", "finish_reason": "stop"},
        ],
        watermark=watermark,
        tail_count=1,
    )

    first = db.merge_side_context(
        side_root_session_id="side",
        destination_session_id="main",
        command_text="/merge",
    )
    assert first["imported_messages"] == 2
    first_packet = db.get_messages_as_conversation("main")[-1]["api_content"]
    assert "CONTEXT COMPACTION" in first_packet
    assert "side q/side a" in first_packet

    db.append_message("side", role="user", content="new q", timestamp=5.0)
    db.append_message("side", role="assistant", content="new a", timestamp=6.0)
    second = db.merge_side_context(
        side_root_session_id="side",
        destination_session_id="main",
        command_text="/merge",
    )
    assert second["imported_messages"] == 2
    second_packet = db.get_messages_as_conversation("main")[-1]["api_content"]
    assert "new q" in second_packet
    assert "new a" in second_packet
    assert "parent q/parent a" not in second_packet

    second_watermark = db.get_active_message_watermark("side")
    db.archive_and_compact(
        "side",
        [
            {
                "role": "user",
                "content": f"{SUMMARY_PREFIX}\nparent and both side turns",
            },
            {"role": "assistant", "content": "new a", "finish_reason": "stop"},
        ],
        watermark=second_watermark,
        tail_count=1,
    )
    third = db.merge_side_context(
        side_root_session_id="side",
        destination_session_id="main",
        command_text="/merge",
    )
    assert third["imported_messages"] == 2
    third_packet = db.get_messages_as_conversation("main")[-1]["api_content"]
    assert "parent and both side turns" in third_packet
    duplicate = db.merge_side_context(
        side_root_session_id="side",
        destination_session_id="main",
        command_text="/merge",
    )
    assert duplicate["status"] == "already_merged"
    assert duplicate["imported_messages"] == 0


def test_merge_rejects_compacted_checkpoint_after_parent_rewind(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("main", source="telegram")
    for role, content, timestamp in (
        ("user", "q1", 1.0),
        ("assistant", "a1", 2.0),
        ("user", "q2", 3.0),
        ("assistant", "a2", 4.0),
    ):
        db.append_message("main", role=role, content=content, timestamp=timestamp)
    parent_history = db.get_messages_as_conversation("main", include_row_ids=True)
    q2_row_id = next(
        message["_row_id"] for message in parent_history if message["content"] == "q2"
    )
    db.create_session(
        "side",
        source="telegram",
        parent_session_id="main",
        model_config={
            "_branched_from": "main",
            "_side_from": "main",
            "_side_root": "side",
            "_side_fork_message_count": len(parent_history),
        },
    )
    db.append_messages_batch("side", parent_history)
    db.append_message("side", role="user", content="side q", timestamp=5.0)
    db.append_message("side", role="assistant", content="side a", timestamp=6.0)
    db.rewind_to_message("main", q2_row_id)
    db.archive_and_compact(
        "side",
        [
            {
                "role": "user",
                "content": f"{SUMMARY_PREFIX}\nq1/a1/q2/a2 and side q/a",
            },
            {"role": "assistant", "content": "side a", "finish_reason": "stop"},
        ],
        watermark=db.get_active_message_watermark("side"),
        tail_count=1,
    )

    with pytest.raises(ValueError, match="main fork context changed"):
        db.merge_side_context(
            side_root_session_id="side",
            destination_session_id="main",
        )


def test_merge_rejects_an_incomplete_side_turn(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    db.append_message("side", role="user", content="unfinished", timestamp=7.0)

    with pytest.raises(ValueError, match="no completed turn"):
        db.merge_side_context(
            destination_session_id="main",
            side_root_session_id="side",
        )


def test_merge_requires_matched_tools_and_a_terminal_assistant(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    db.append_message("side", role="user", content="use a tool", timestamp=7.0)
    db.append_message(
        "side",
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
        finish_reason="tool_calls",
        timestamp=8.0,
    )

    with pytest.raises(ValueError, match="no completed turn"):
        db.merge_side_context(
            destination_session_id="main",
            side_root_session_id="side",
        )

    db.append_message(
        "side",
        role="tool",
        content="tool result",
        tool_name="lookup",
        tool_call_id="call-1",
        timestamp=9.0,
    )
    db.append_message(
        "side",
        role="assistant",
        content="finished",
        finish_reason="stop",
        timestamp=10.0,
    )

    merged = db.merge_side_context(
        destination_session_id="main",
        side_root_session_id="side",
    )
    assert merged["status"] == "merged"
    assert merged["imported_messages"] == 6


def test_merge_rejects_an_active_main_turn_lease(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    holder = "pid=1:turn=active-main"
    assert db.acquire_session_turn_lease("main", holder, ttl_seconds=60)

    with pytest.raises(RuntimeError, match="active turn lease"):
        db.merge_side_context(
            destination_session_id="main",
            side_root_session_id="side",
        )

    db.release_session_turn_lease("main", holder)


def test_merge_follows_main_and_side_compression_tips(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_side(db)
    db.end_session("main", end_reason="compression")
    db.create_session("main-tip", source="telegram", parent_session_id="main")
    db.append_message("main-tip", role="user", content="continued main", timestamp=5.0)
    db.append_message("main-tip", role="assistant", content="continued answer", timestamp=6.0)
    db.end_session("side", end_reason="compression")
    db.create_session("side-tip", source="telegram", parent_session_id="side")
    db.append_message("side-tip", role="user", content="continued side", timestamp=7.0)
    db.append_message("side-tip", role="assistant", content="side tip answer", timestamp=8.0)

    result = db.merge_side_context(
        destination_session_id="main-tip",
        side_root_session_id="side",
    )

    assert result["destination_session_id"] == "main-tip"
    assert result["side_tip_session_id"] == "side-tip"
    receipt = db.get_messages_as_conversation("main-tip")[-1]
    assert "side question" in receipt["api_content"]
    assert "side tip answer" in receipt["api_content"]


@pytest.mark.asyncio
async def test_gateway_merge_routes_completion_back_to_main():
    runner = GatewayRunner.__new__(GatewayRunner)
    parent_route = "agent:default:telegram:dm:chat"
    origin = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user",
        chat_id="chat",
        chat_type="dm",
    )
    parent_entry = SessionEntry(
        session_key=parent_route,
        session_id="main",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=origin,
        platform=Platform.TELEGRAM,
    )
    sync_store = MagicMock()
    runner.session_store = sync_store
    runner._async_session_store = SimpleNamespace(
        _store=sync_store,
        lookup_by_session_key=AsyncMock(return_value=parent_entry),
    )
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(
            return_value={
                "model_config": json.dumps(
                    {
                        "_side_from": "main",
                        "_side_root": "side",
                        "_side_parent_route": parent_route,
                    }
                )
            }
        ),
        merge_side_context=AsyncMock(
            return_value={"status": "merged", "message": "merged into main"}
        ),
    )
    runner._is_session_running = MagicMock(return_value=False)
    runner._evict_cached_agent = MagicMock()
    event = MessageEvent(
        text="/merge",
        source=origin,
        metadata={
            "gateway_explicit_session_route": True,
            "gateway_session_key": parent_route + ":side:side",
            "gateway_session_id": "side",
            "gateway_session_strict": True,
            "gateway_side_root_session_id": "side",
        },
    )

    result = await runner._handle_merge_command(event)

    assert result == "merged into main"
    runner._session_db.merge_side_context.assert_awaited_once_with(
        destination_session_id="main",
        side_root_session_id="side",
        command_text="/merge",
    )
    runner._evict_cached_agent.assert_called_once_with(parent_route)
    assert "gateway_explicit_session_route" not in event.metadata
    assert "gateway_side_root_session_id" not in event.metadata


@pytest.mark.asyncio
async def test_gateway_merge_requires_a_side_reply():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = object()
    event = MessageEvent(
        text="/merge",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="user",
            chat_id="chat",
            chat_type="dm",
        ),
    )

    assert await runner._handle_merge_command(event) == (
        "Reply to a side-session message with `/merge`."
    )