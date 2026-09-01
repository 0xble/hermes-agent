"""Gateway contextual /side command regressions."""

import asyncio
import json
import re
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.agent_runtime_helpers import repair_message_sequence
from gateway.platforms.base import MessageEvent, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from hermes_cli.commands import resolve_command
from hermes_state import AsyncSessionDB, SessionDB


def _event(text: str = "/side investigate this") -> MessageEvent:
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
    side_adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        supports_code_blocks=True,
    )
    object.__setattr__(runner, "_side_adapter", side_adapter)
    runner._adapter_for_source = MagicMock(return_value=side_adapter)

    sync_store = MagicMock()
    async_store = SimpleNamespace(
        _store=sync_store,
        get_or_create_session=AsyncMock(return_value=_parent_entry()),
        load_transcript=AsyncMock(return_value=history or []),
        switch_session=AsyncMock(),
        bind_session_route=AsyncMock(return_value=SimpleNamespace(session_id="side-child")),
        close_session_route=AsyncMock(return_value="side-child"),
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


def test_side_command_is_gateway_dispatchable():
    command = resolve_command("side")

    assert command is not None
    assert command.name == "side"
    assert command.gateway_only is True
    assert command.busy_policy == "dispatch"
    assert command.busy_handler == "side"


def test_merge_command_is_gateway_only_and_non_interrupting():
    command = resolve_command("merge")
    alias = resolve_command("fold")

    assert command is not None
    assert command.name == "merge"
    assert command.gateway_only is True
    assert command.busy_policy == "reject"
    assert alias is command


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
def test_side_snapshot_excludes_provisional_assistant_turns(finish_reason):
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

    assert GatewayRunner._completed_side_history(history) == history[:2]


@pytest.mark.asyncio
async def test_side_requires_a_prompt():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = object()

    result = await runner._handle_side_command(_event("/side"))

    assert result == "Usage: /side <prompt>"


@pytest.mark.asyncio
async def test_side_requires_a_completed_parent_turn():
    runner, _store = _runner(history=[{"role": "user", "content": "pending"}])

    result = await runner._handle_side_command(_event())

    assert result == (
        "No completed conversation to fork. Wait for the current turn "
        "to finish, then try again."
    )
    runner._session_db.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_side_clones_only_the_last_completed_turn_and_keeps_parent_active():
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

    result = await runner._handle_side_command(event)
    await asyncio.gather(*runner._background_tasks)

    runner._normalize_source_for_session_key.assert_called_once_with(event.source)
    store.get_or_create_session.assert_awaited_once()
    store.switch_session.assert_not_awaited()

    create_kwargs = runner._session_db.create_session.await_args.kwargs
    child_session_id = create_kwargs["session_id"]
    assert child_session_id.startswith("side_")
    assert create_kwargs["parent_session_id"] == "parent-1"
    assert create_kwargs["model"] == "parent-model"
    assert create_kwargs["session_key"].endswith(f":side:{child_session_id}")
    assert create_kwargs["thread_id"] == "42"
    assert create_kwargs["cwd"] == "/tmp/project"
    assert create_kwargs["profile_name"] == "default"
    assert create_kwargs["git_repo_root"] == "/tmp/project"
    assert create_kwargs["model_config"] == {
        "_branched_from": "parent-1",
        "_side_from": "parent-1",
        "_side_root": child_session_id,
        "_side_parent_route": _parent_entry().session_key,
        "_side_fork_message_count": 2,
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

    side_event = getattr(runner, "_side_adapter").handle_message.call_args.args[0]
    assert side_event.text == "investigate this"
    assert side_event.source.thread_id == "42"
    assert side_event.metadata["gateway_session_id"] == child_session_id
    assert side_event.metadata["gateway_explicit_session_route"] is True
    side_id = child_session_id.rsplit("_", 1)[-1]
    assert result == (
        f"↗️ Side `{side_id}` started\n"
        "*Reply here to continue*"
    )


@pytest.mark.asyncio
async def test_side_fails_closed_when_context_copy_fails():
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    runner, _store = _runner(history=history, copy_error=RuntimeError("db locked"))

    result = await runner._handle_side_command(_event())

    assert result == "❌ Side failed: could not copy the parent context."
    runner._run_background_task.assert_not_called()
    runner._session_db.end_session.assert_awaited_once()
    assert runner._session_db.end_session.await_args.kwargs["end_reason"] == "side_copy_failed"


@pytest.mark.asyncio
async def test_side_dispatches_while_parent_agent_is_busy():
    runner, _store = _runner(history=[])
    runner._handle_side_command = AsyncMock(return_value="sideed")
    runner._peek_session_state = MagicMock(
        return_value=SimpleNamespace(
            persistent=SimpleNamespace(run_generation=7),
        )
    )
    event = _event()

    result = await runner._dispatch_busy_slash_command(
        event,
        resolve_command("side"),
        "telegram:parent",
        event.source,
    )

    assert result == "sideed"
    runner._handle_side_command.assert_awaited_once_with(
        event,
        active_session_key="telegram:parent",
        active_run_generation=7,
    )


def test_active_turn_side_checkpoint_requires_every_tool_result():
    pending = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "first", "arguments": "{}"}},
                {"id": "call-2", "function": {"name": "second", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "first result"},
    ]

    assert GatewayRunner._active_turn_side_history(pending) == []

    complete = pending + [
        {"role": "tool", "tool_call_id": "call-2", "content": "second result"}
    ]
    assert GatewayRunner._active_turn_side_history(complete) == complete


@pytest.mark.parametrize(
    "tool_calls,tool_results",
    [
        (
            [{"id": "call-1", "function": {"name": "one", "arguments": "{}"}}],
            [
                {"role": "tool", "tool_call_id": "call-1", "content": "one"},
                {"role": "tool", "tool_call_id": "call-1", "content": "duplicate"},
            ],
        ),
        (
            [{"id": "call-1", "function": {"name": "one", "arguments": "{}"}}],
            [{"role": "tool", "content": "missing id"}],
        ),
        (
            [
                {
                    "id": "response-1",
                    "call_id": "shared",
                    "function": {"name": "one", "arguments": "{}"},
                },
                {
                    "id": "shared",
                    "call_id": "call-2",
                    "function": {"name": "two", "arguments": "{}"},
                },
            ],
            [
                {"role": "tool", "tool_call_id": "shared", "content": "ambiguous"},
                {"role": "tool", "tool_call_id": "call-2", "content": "two"},
            ],
        ),
    ],
)
def test_active_turn_side_checkpoint_rejects_non_bijective_tool_results(
    tool_calls,
    tool_results,
):
    history = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": tool_calls,
        },
        *tool_results,
    ]

    assert GatewayRunner._active_turn_side_history(history) == []


def test_active_turn_side_checkpoint_accepts_codex_call_id_alias():
    history = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "response-item-1",
                    "call_id": "call-1",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]

    assert GatewayRunner._active_turn_side_history(history) == history


def test_active_turn_checkpoint_accepts_side_prompt_without_repair():
    history = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {"role": "user", "content": "side prompt"},
    ]
    original = [dict(message) for message in history]

    assert repair_message_sequence(SimpleNamespace(), history) == 0
    assert history == original


def test_active_turn_side_checkpoint_prefers_completed_assistant_response():
    history = [
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "done", "finish_reason": "stop"},
    ]

    assert GatewayRunner._active_turn_side_history(history) == history


def test_active_turn_checkpoint_waits_for_work_after_intermediate_response():
    history = [
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "checking", "finish_reason": "stop"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-latest",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
    ]

    assert GatewayRunner._active_turn_side_history(history) == []


@pytest.mark.asyncio
async def test_busy_side_queues_then_edits_status_at_tool_checkpoint():
    pending = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "lookup", "arguments": "{}"}}
            ],
        },
    ]
    complete = pending + [
        {"role": "tool", "tool_call_id": "call-1", "content": "result"}
    ]
    runner, store = _runner(history=pending)
    store.load_transcript.side_effect = [pending, complete]
    active_state = SimpleNamespace(
        turn=SimpleNamespace(agent=object()),
        persistent=SimpleNamespace(run_generation=7),
    )
    runner._peek_session_state = MagicMock(return_value=active_state)
    runner._thread_metadata_for_source = MagicMock(return_value={"thread_id": "42"})
    status_adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        supports_code_blocks=True,
        send_or_update_status=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="status-1")
        )
    )
    runner._adapter_for_source = MagicMock(return_value=status_adapter)

    result = await runner._handle_side_command(
        _event('/side change the icon to a brain'),
        active_session_key="telegram:parent",
        active_run_generation=7,
    )
    waiters = list(runner._background_tasks)
    await asyncio.gather(*waiters)

    assert result == ""
    assert store.get_or_create_session.await_count == 1
    assert store.load_transcript.await_count == 2
    assert status_adapter.send_or_update_status.await_count == 2
    queued_call, started_call = status_adapter.send_or_update_status.await_args_list
    assert queued_call.args[1] == started_call.args[1]
    assert queued_call.args[2] == (
        '↗️ Side queued: "change the icon to a brain"\n'
        "*Waiting for the current tool step to finish.*"
    )
    assert re.fullmatch(
        r"↗️ Side `[0-9a-f]{6}` started\n\*Reply here to continue\*",
        started_call.args[2],
    )
    copied_rows = runner._session_db.append_messages_batch.await_args.args[1]
    assert [row["role"] for row in copied_rows] == ["user", "assistant", "tool"]
    assert status_adapter.handle_message.await_count == 1
    side_event = status_adapter.handle_message.await_args.args[0]
    assert side_event.text == "change the icon to a brain"
    assert side_event.metadata["gateway_explicit_session_route"] is True


@pytest.mark.asyncio
async def test_busy_side_edits_queued_status_to_safe_abort():
    pending = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "lookup", "arguments": "{}"}}
            ],
        },
    ]
    runner, store = _runner(history=pending)
    store.load_transcript.side_effect = [pending, pending]
    active_state = SimpleNamespace(
        turn=SimpleNamespace(agent=object()),
        persistent=SimpleNamespace(run_generation=7),
    )
    stopped_state = SimpleNamespace(
        turn=SimpleNamespace(agent=None),
        persistent=SimpleNamespace(run_generation=7),
    )
    runner._peek_session_state = MagicMock(
        side_effect=[active_state, active_state, active_state, stopped_state]
    )
    runner._thread_metadata_for_source = MagicMock(return_value={"thread_id": "42"})
    status_adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        supports_code_blocks=True,
        send_or_update_status=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="status-1")
        )
    )
    runner._adapter_for_source = MagicMock(return_value=status_adapter)

    result = await runner._handle_side_command(
        _event('/side change the icon to a brain'),
        active_session_key="telegram:parent",
        active_run_generation=7,
    )
    waiters = list(runner._background_tasks)
    await asyncio.gather(*waiters)

    assert result == ""
    assert status_adapter.send_or_update_status.await_count == 2
    aborted = status_adapter.send_or_update_status.await_args_list[1].args[2]
    assert aborted == (
        "⚠️ Side aborted: the current turn stopped before its context could "
        "be copied. Nothing was changed."
    )
    runner._session_db.create_session.assert_not_awaited()
    runner._run_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_busy_side_aborts_if_turn_generation_changes_during_context_read():
    complete = [
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "done", "finish_reason": "stop"},
    ]
    runner, _store = _runner(history=complete)
    generation_7 = SimpleNamespace(
        turn=SimpleNamespace(agent=object()),
        persistent=SimpleNamespace(run_generation=7),
    )
    generation_8 = SimpleNamespace(
        turn=SimpleNamespace(agent=object()),
        persistent=SimpleNamespace(run_generation=8),
    )
    runner._peek_session_state = MagicMock(
        side_effect=[generation_7, generation_8]
    )

    result = await runner._handle_side_command(
        _event('/side change the icon to a brain'),
        active_session_key="telegram:parent",
        active_run_generation=7,
    )

    assert result == (
        "⚠️ Side aborted: the current turn stopped before its context could "
        "be copied. Nothing was changed."
    )
    runner._session_db.create_session.assert_not_awaited()
    runner._run_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_busy_side_uses_final_checkpoint_after_same_turn_releases():
    complete = [
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "done", "finish_reason": "stop"},
    ]
    runner, _store = _runner(history=complete)
    active = SimpleNamespace(
        turn=SimpleNamespace(agent=object()),
        persistent=SimpleNamespace(run_generation=7),
    )
    released = SimpleNamespace(
        turn=SimpleNamespace(agent=None),
        persistent=SimpleNamespace(run_generation=7),
    )
    runner._peek_session_state = MagicMock(side_effect=[active, released])

    result = await runner._handle_side_command(
        _event('/side change the icon to a brain'),
        active_session_key="telegram:parent",
        active_run_generation=7,
    )

    assert re.fullmatch(
        r"↗️ Side `[0-9a-f]{6}` started\n\*Reply here to continue\*",
        result,
    )
    runner._session_db.create_session.assert_awaited_once()
    await asyncio.gather(*runner._background_tasks)
    assert getattr(runner, "_side_adapter").handle_message.await_count == 1


@pytest.mark.asyncio
async def test_side_cancellation_finalizes_created_child_before_worker_registration():
    history = [
        {"role": "user", "content": "parent request"},
        {"role": "assistant", "content": "done", "finish_reason": "stop"},
    ]
    runner, _store = _runner(history=history)
    runner._session_db.append_messages_batch.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await runner._handle_side_command(_event('/side do the side task'))

    runner._session_db.create_session.assert_awaited_once()
    runner._session_db.end_session.assert_awaited_once()
    assert runner._session_db.end_session.await_args.kwargs == {
        "end_reason": "side_launch_cancelled"
    }
    runner._run_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_during_to_thread_creation_drains_before_finalizing(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    release_create = threading.Event()
    create_started = threading.Event()
    child_ids: list[str] = []
    try:
        db.create_session(
            "parent-1",
            "telegram",
            session_key="agent:default:telegram:dm:67890",
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
        )
        original_create = db.create_session

        def delayed_create(session_id, *args, **kwargs):
            if str(session_id).startswith("side_"):
                child_ids.append(session_id)
                create_started.set()
                release_create.wait(timeout=5)
            return original_create(session_id, *args, **kwargs)

        db.create_session = delayed_create
        runner = GatewayRunner.__new__(GatewayRunner)
        object.__setattr__(
            runner, "config", SimpleNamespace(multiplex_profiles=False)
        )
        runner._session_db = AsyncSessionDB(db)
        runner._background_tasks = set()
        runner._run_background_task = AsyncMock()
        runner._reply_anchor_for_event = MagicMock(return_value="msg-1")
        runner._normalize_source_for_session_key = MagicMock(
            side_effect=lambda source: source
        )
        runner.session_store = MagicMock()
        runner._async_session_store = SimpleNamespace(  # type: ignore[assignment]
            _store=runner.session_store,
            get_or_create_session=AsyncMock(return_value=_parent_entry()),
            load_transcript=AsyncMock(
                return_value=[
                    {"role": "user", "content": "parent request"},
                    {
                        "role": "assistant",
                        "content": "done",
                        "finish_reason": "stop",
                    },
                ]
            ),
        )

        launch = asyncio.create_task(runner._handle_side_command(_event()))
        assert await asyncio.to_thread(create_started.wait, 5)
        launch.cancel()
        release_create.set()
        with pytest.raises(asyncio.CancelledError):
            await launch

        assert len(child_ids) == 1
        child = db.get_session(child_ids[0])
        assert child is not None
        assert child["ended_at"] is not None
        assert child["end_reason"] == "side_launch_cancelled"
        runner._run_background_task.assert_not_called()
    finally:
        release_create.set()
        db.close()


@pytest.mark.asyncio
async def test_cancel_side_waiters_does_not_abort_waiter_it_did_not_cancel():
    runner, _store = _runner(history=[])
    status_adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        send_or_update_status=AsyncMock(return_value=SimpleNamespace(success=True))
    )
    runner._adapter_for_source = MagicMock(return_value=status_adapter)

    class CompletedRace:
        def done(self):
            return False

        def cancel(self):
            return False

    waiter = CompletedRace()
    runner._side_waiter_tasks = {waiter}
    runner._side_waiter_statuses = {
        waiter: {
            "event": _event(),
            "source": _event().source,
            "status_key": "side:test",
            "terminal": True,
        }
    }

    await runner._cancel_side_waiters()

    status_adapter.send_or_update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_side_finalization_drains_through_repeated_cancellation():
    end_started = asyncio.Event()
    release_end = asyncio.Event()
    ended = False

    async def end_session(_session_id, *, end_reason):
        nonlocal ended
        assert end_reason == "side_launch_cancelled"
        end_started.set()
        await release_end.wait()
        ended = True

    finalization = asyncio.create_task(
        GatewayRunner._finalize_incomplete_side(
            SimpleNamespace(end_session=end_session),
            "side-child",
            "side_launch_cancelled",
        )
    )
    await end_started.wait()
    finalization.cancel()
    await asyncio.sleep(0)
    finalization.cancel()
    await asyncio.sleep(0)
    assert not finalization.done()
    release_end.set()
    await finalization

    assert ended is True


@pytest.mark.asyncio
async def test_cancel_side_waiters_edits_queued_status_before_shutdown():
    pending = [
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
    ]
    runner, _store = _runner(history=pending)
    active = SimpleNamespace(
        turn=SimpleNamespace(agent=object()),
        persistent=SimpleNamespace(run_generation=7),
    )
    runner._peek_session_state = MagicMock(return_value=active)
    runner._thread_metadata_for_source = MagicMock(return_value={"thread_id": "42"})
    status_adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        send_or_update_status=AsyncMock(return_value=SimpleNamespace(success=True))
    )
    runner._adapter_for_source = MagicMock(return_value=status_adapter)

    result = await runner._handle_side_command(
        _event('/side change the icon to a brain'),
        active_session_key="telegram:parent",
        active_run_generation=7,
    )
    await runner._cancel_side_waiters()

    assert result == ""
    assert status_adapter.send_or_update_status.await_count == 2
    assert status_adapter.send_or_update_status.await_args_list[1].args[2] == (
        "⚠️ Side aborted: the current turn stopped before its context could "
        "be copied. Nothing was changed."
    )
    assert not runner._side_waiter_tasks
    assert not runner._side_waiter_statuses


@pytest.mark.asyncio
async def test_side_appears_in_gateway_help():
    runner, _store = _runner(history=[])

    result = await runner._handle_help_command(_event("/help"))

    assert "/side" in result


@pytest.mark.asyncio
async def test_side_persists_a_real_independent_child(tmp_path):
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
        object.__setattr__(
            runner, "config", SimpleNamespace(multiplex_profiles=False)
        )
        runner._session_db = AsyncSessionDB(db)
        runner._background_tasks = set()
        runner._run_background_task = AsyncMock()
        runner._reply_anchor_for_event = MagicMock(return_value="msg-1")
        runner._normalize_source_for_session_key = MagicMock(
            side_effect=lambda source: source
        )
        store = MagicMock()
        runner.session_store = store
        runner._async_session_store = SimpleNamespace(  # type: ignore[assignment]
            _store=store,
            get_or_create_session=AsyncMock(return_value=_parent_entry()),
            load_transcript=AsyncMock(return_value=history),
            bind_session_route=AsyncMock(return_value=SimpleNamespace(session_id="side-child")),
            close_session_route=AsyncMock(return_value="side-child"),
        )
        side_adapter = SimpleNamespace(
            handle_message=AsyncMock(),
            supports_code_blocks=True,
        )
        runner._adapter_for_source = MagicMock(return_value=side_adapter)

        await runner._handle_side_command(_event())
        await asyncio.gather(*runner._background_tasks)

        side_event = side_adapter.handle_message.await_args.args[0]
        child_id = side_event.metadata["gateway_session_id"]
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
        assert child["title"].startswith("Side ")
        assert child["session_key"].endswith(f":side:{child_id}")
        assert child["cwd"] == "/tmp/project"
        model_config = json.loads(child["model_config"])
        assert model_config["_side_from"] == "parent-1"
        assert model_config["_side_root"] == child_id
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
