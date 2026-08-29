"""Gateway contextual /spawn command regressions."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import MessageEvent, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from hermes_cli.commands import resolve_command
from hermes_state import AsyncSessionDB, SessionDB


def _event(text: str = "/spawn investigate this") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            user_name="testuser",
            thread_id="",
        ),
        message_id="msg-1",
    )


def _parent_entry() -> SessionEntry:
    origin = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="12345",
        chat_id="67890",
        chat_type="dm",
        user_name="testuser",
        thread_id="42",
    )
    return SessionEntry(
        session_key="agent:default:telegram:dm:67890:42",
        session_id="parent-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=origin,
        display_name="Current topic",
        platform=Platform.TELEGRAM,
    )


def _runner(*, history=None, copy_error=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = {"model": {"default": "test-model"}}
    runner._background_tasks = set()
    runner._reply_anchor_for_event = MagicMock(return_value="reply-1")
    runner._normalize_source_for_session_key = MagicMock(
        return_value=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            user_name="testuser",
            thread_id="42",
        )
    )
    runner._run_background_task = AsyncMock()

    sync_store = MagicMock()
    async_store = SimpleNamespace(
        _store=sync_store,
        get_or_create_session=AsyncMock(return_value=_parent_entry()),
        load_transcript=AsyncMock(return_value=history or []),
        switch_session=AsyncMock(),
    )
    runner.session_store = sync_store
    runner._async_session_store = async_store

    append_messages_batch = AsyncMock()
    if copy_error is not None:
        append_messages_batch.side_effect = copy_error
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(
            return_value={
                "id": "parent-1",
                "model": "parent-model",
                "cwd": "/tmp/project",
                "profile_name": "default",
                "git_repo_root": "/tmp/project",
            }
        ),
        create_session=AsyncMock(),
        append_messages_batch=append_messages_batch,
        set_session_title=AsyncMock(),
        end_session=AsyncMock(),
    )
    return runner, async_store


@pytest.mark.parametrize("name", ["spawn", "side"])
def test_spawn_command_is_gateway_dispatchable(name):
    command = resolve_command(name)

    assert command is not None
    assert command.name == "spawn"
    assert command.gateway_only is True
    assert command.busy_policy == "dispatch"


@pytest.mark.parametrize(
    "finish_reason",
    [
        "verification_required",
        "verify_hook_continue",
        "incomplete",
        "length",
        "max_tokens",
        "tool_calls",
        "function_call",
    ],
)
def test_spawn_snapshot_excludes_provisional_assistant_turns(finish_reason):
    history = [
        {"role": "user", "content": "completed question"},
        {
            "role": "assistant",
            "content": "completed answer",
            "finish_reason": "stop",
        },
        {"role": "user", "content": "active question"},
        {
            "role": "assistant",
            "content": "provisional answer",
            "finish_reason": finish_reason,
        },
    ]

    assert GatewayRunner._completed_spawn_history(history) == history[:2]


@pytest.mark.asyncio
async def test_spawn_requires_a_prompt():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = object()

    result = await runner._handle_spawn_command(_event("/spawn"))

    assert result == "Usage: /spawn <prompt>"


@pytest.mark.asyncio
async def test_spawn_requires_a_completed_parent_turn():
    runner, _store = _runner(history=[{"role": "user", "content": "pending"}])

    result = await runner._handle_spawn_command(_event())

    assert "no completed conversation" in result.lower()
    runner._session_db.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_spawn_clones_only_the_last_completed_turn_and_keeps_parent_active():
    history = [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        {
            "role": "assistant",
            "content": "first answer",
            "api_content": "cached-provider-shape",
            "finish_reason": "stop",
            "platform_message_id": "platform-2",
            "observed": True,
            "effect_disposition": "unknown",
            "display_kind": "internal_notification",
            "display_metadata": {"source": "test"},
            "timestamp": 2.0,
        },
        {"role": "user", "content": "active question", "timestamp": 3.0},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
            "timestamp": 4.0,
        },
        {
            "role": "tool",
            "content": "partial result",
            "tool_call_id": "call-1",
            "timestamp": 5.0,
        },
    ]
    runner, store = _runner(history=history)
    event = _event()

    result = await runner._handle_spawn_command(event)
    await asyncio.gather(*runner._background_tasks)

    runner._normalize_source_for_session_key.assert_called_once_with(event.source)
    store.get_or_create_session.assert_awaited_once()
    store.switch_session.assert_not_awaited()

    create_kwargs = runner._session_db.create_session.await_args.kwargs
    child_session_id = create_kwargs["session_id"]
    assert child_session_id.startswith("spawn_")
    assert create_kwargs["parent_session_id"] == "parent-1"
    assert create_kwargs["model"] == "parent-model"
    assert create_kwargs["session_key"] is None
    assert create_kwargs["thread_id"] == "42"
    assert create_kwargs["cwd"] == "/tmp/project"
    assert create_kwargs["profile_name"] == "default"
    assert create_kwargs["git_repo_root"] == "/tmp/project"
    assert create_kwargs["model_config"] == {
        "_branched_from": "parent-1",
        "_spawned_from": "parent-1",
        "_spawn_mode": "one_shot",
    }
    assert json.loads(create_kwargs["origin_json"])["thread_id"] == "42"

    copied_rows = runner._session_db.append_messages_batch.await_args.args[1]
    assert [(row["role"], row["content"]) for row in copied_rows] == [
        ("user", "first question"),
        ("assistant", "first answer"),
    ]
    assert copied_rows[1]["api_content"] == "cached-provider-shape"
    assert copied_rows[1]["finish_reason"] == "stop"
    assert copied_rows[1]["platform_message_id"] == "platform-2"
    assert copied_rows[1]["observed"] is True
    assert copied_rows[1]["effect_disposition"] == "unknown"
    assert copied_rows[1]["display_kind"] == "internal_notification"
    assert copied_rows[1]["display_metadata"] == {"source": "test"}

    spawn_kwargs = runner._run_background_task.await_args.kwargs
    assert spawn_kwargs["prompt"] == "investigate this"
    assert spawn_kwargs["source"].thread_id == "42"
    assert spawn_kwargs["event_message_id"] is None
    assert spawn_kwargs["task_id"] == child_session_id
    assert spawn_kwargs["parent_session_id"] == "parent-1"
    assert spawn_kwargs["task_kind"] == "spawn"
    task_title = spawn_kwargs["task_title"]
    assert task_title.startswith("Spawn ")
    assert task_title.endswith(": investigate this")
    assert spawn_kwargs["conversation_history"] == history[:2]
    assert "Spawn started" in result
    assert f'Child session: "{task_title}"' in result
    assert child_session_id not in result


@pytest.mark.asyncio
async def test_spawn_fails_closed_when_context_copy_fails():
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    runner, _store = _runner(history=history, copy_error=RuntimeError("db locked"))

    result = await runner._handle_spawn_command(_event())

    assert "could not copy" in result.lower()
    runner._run_background_task.assert_not_called()
    runner._session_db.end_session.assert_awaited_once()
    assert runner._session_db.end_session.await_args.kwargs["end_reason"] == "spawn_copy_failed"


@pytest.mark.asyncio
async def test_spawn_dispatches_while_parent_agent_is_busy():
    runner, _store = _runner(history=[])
    runner._handle_spawn_command = AsyncMock(return_value="spawned")
    event = _event()

    result = await runner._dispatch_busy_slash_command(
        event,
        resolve_command("spawn"),
        "telegram:parent",
        event.source,
    )

    assert result == "spawned"
    runner._handle_spawn_command.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_spawn_appears_in_gateway_help():
    runner, _store = _runner(history=[])

    result = await runner._handle_help_command(_event("/help"))

    assert "/spawn" in result


@pytest.mark.asyncio
async def test_spawn_persists_a_real_independent_child(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(
            "parent-1",
            "telegram",
            model="parent-model",
            session_key="agent:default:telegram:dm:67890:42",
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="42",
            cwd="/tmp/project",
        )
        db.append_message("parent-1", "user", "parent question")
        db.append_message(
            "parent-1",
            "assistant",
            "parent answer",
            finish_reason="stop",
            platform_message_id="platform-parent-answer",
            observed=True,
            effect_disposition="unknown",
            display_kind="internal_notification",
            display_metadata={"source": "integration"},
        )
        history = db.get_messages_as_conversation("parent-1")

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = SimpleNamespace(multiplex_profiles=False)
        runner._session_db = AsyncSessionDB(db)
        runner._background_tasks = set()
        runner._run_background_task = AsyncMock()
        runner._reply_anchor_for_event = MagicMock(return_value="msg-1")
        runner._normalize_source_for_session_key = MagicMock(
            side_effect=lambda source: source
        )
        store = MagicMock()
        runner.session_store = store
        runner._async_session_store = SimpleNamespace(
            _store=store,
            get_or_create_session=AsyncMock(return_value=_parent_entry()),
            load_transcript=AsyncMock(return_value=history),
        )

        await runner._handle_spawn_command(_event())
        await asyncio.gather(*runner._background_tasks)

        await_call = runner._run_background_task.await_args
        assert await_call is not None
        child_id = await_call.kwargs["task_id"]
        child = db.get_session(child_id)
        assert child is not None
        child_messages = db.get_messages_as_conversation(child_id)
        child_rows = db.get_messages(child_id)
        parent = db.get_session("parent-1")
        parent_messages = db.get_messages_as_conversation("parent-1")

        assert parent is not None
        assert parent["ended_at"] is None
        assert [(m["role"], m["content"]) for m in parent_messages] == [
            ("user", "parent question"),
            ("assistant", "parent answer"),
        ]
        assert child["parent_session_id"] == "parent-1"
        assert child["title"] == await_call.kwargs["task_title"]
        assert child["session_key"] is None
        assert child["cwd"] == "/tmp/project"
        assert json.loads(child["model_config"])["_spawned_from"] == "parent-1"
        assert [(m["role"], m["content"]) for m in child_messages] == [
            ("user", "parent question"),
            ("assistant", "parent answer"),
        ]
        child_answer = child_rows[1]
        assert child_answer["finish_reason"] == "stop"
        assert child_answer["platform_message_id"] == "platform-parent-answer"
        assert bool(child_answer["observed"]) is True
        assert child_answer["effect_disposition"] == "unknown"
        assert child_answer["display_kind"] == "internal_notification"
        assert child_answer["display_metadata"] == {"source": "integration"}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pre_agent_failure_ends_real_spawn_child(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(
            "spawn-real-failure",
            "telegram",
            model="parent-model",
            parent_session_id=None,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="42",
            model_config={"_spawned_from": "parent-1"},
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        object.__setattr__(
            runner, "config", SimpleNamespace(multiplex_profiles=False)
        )
        runner._session_db = AsyncSessionDB(db)
        setattr(
            runner,
            "_resolve_session_agent_runtime",
            MagicMock(return_value=("test-model", {"api_key": None})),
        )
        adapter = AsyncMock()
        adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}
        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="42",
        )

        await runner._run_background_task(
            "test prompt",
            source,
            "spawn-real-failure",
            task_kind="spawn",
        )

        child = db.get_session("spawn-real-failure")
        assert child is not None
        assert child["ended_at"] is not None
        assert child["end_reason"] == "spawn_no_credentials"
    finally:
        db.close()
