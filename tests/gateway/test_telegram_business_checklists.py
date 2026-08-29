"""Telegram Business routing and native checklist regressions (HERMES-063)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

telegram_adapter = load_plugin_adapter("telegram")
TelegramAdapter = telegram_adapter.TelegramAdapter


def _source(connection_id: str | None = None) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
        user_id="42",
        business_connection_id=connection_id,
    )


def _adapter():
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={"allow_from": ["*"]})
    )
    adapter._rich_messages_enabled = False
    adapter.send_typing = AsyncMock()
    return adapter


def _business_checklist_message():
    checklist = SimpleNamespace(
        title="Launch",
        title_entities=[],
        tasks=[
            SimpleNamespace(
                id=1,
                text="Venue",
                text_entities=[],
                completed_by_user=None,
                completed_by_chat=None,
                completion_date=None,
            )
        ],
        others_can_add_tasks=False,
        others_can_mark_tasks_as_done=True,
    )
    return SimpleNamespace(
        message_id=77,
        chat=SimpleNamespace(
            id=123,
            type="private",
            title=None,
            full_name="Brian",
            is_forum=False,
        ),
        from_user=SimpleNamespace(id=42, full_name="Brian", username="brian"),
        sender_chat=None,
        business_connection_id="biz-A",
        checklist=checklist,
        checklist_tasks_added=None,
        checklist_tasks_done=None,
        text=None,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        reply_to_message=None,
        quote=None,
        date=datetime.now(timezone.utc),
        forum_topic_created=None,
    )


def test_business_connection_round_trips_and_isolates_session_keys():
    source = _source("biz-A")
    other = _source("biz-B")
    ordinary = _source()

    assert build_session_key(source) != build_session_key(other)
    assert build_session_key(ordinary) == "agent:main:telegram:dm:123"
    restored = SessionSource.from_dict(source.to_dict())
    assert restored.business_connection_id == "biz-A"
    assert build_session_key(restored) == build_session_key(source)


def test_inbound_business_message_preserves_connection_on_source_and_event_metadata():
    adapter = _adapter()

    event = adapter._build_checklist_event(_business_checklist_message(), update_id=100)

    assert event.source.business_connection_id == "biz-A"
    assert event.metadata["telegram_business_connection_id"] == "biz-A"


def test_runner_stamps_business_connection_into_every_turn_reply():
    runner = object.__new__(GatewayRunner)

    metadata = runner._thread_metadata_for_source(_source("biz-A"))

    assert metadata == {"telegram_business_connection_id": "biz-A"}


def test_plain_text_send_forwards_business_connection_id():
    adapter = _adapter()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)))
    adapter._bot = bot

    async def _run(cooldown_chat_id, send_fn, *args, **kwargs):
        return await send_fn(*args, **kwargs)

    adapter._run_send_call = _run

    result = asyncio.run(
        adapter.send(
            "123",
            "hello",
            metadata={"telegram_business_connection_id": "biz-A", "notify": True},
        )
    )

    assert result.success is True
    assert result.message_id == "88"
    assert bot.send_message.await_args.kwargs["business_connection_id"] == "biz-A"


def test_business_routing_reaches_control_messages():
    adapter = _adapter()
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=89))
    )
    adapter._bot = bot

    async def _run(cooldown_chat_id, send_fn, *args, **kwargs):
        return await send_fn(*args, **kwargs)

    adapter._run_send_call = _run

    result = asyncio.run(
        adapter.send_update_prompt(
            "123",
            "Continue?",
            metadata={"telegram_business_connection_id": "biz-A"},
        )
    )

    assert result.success is True
    assert bot.send_message.await_args.kwargs["business_connection_id"] == "biz-A"


def test_business_routing_reaches_media_messages(tmp_path):
    adapter = _adapter()
    bot = SimpleNamespace(
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=90))
    )
    adapter._bot = bot
    document = tmp_path / "evidence.txt"
    document.write_text("evidence")

    async def _run(cooldown_chat_id, send_fn, *args, **kwargs):
        return await send_fn(*args, **kwargs)

    adapter._run_send_call = _run

    result = asyncio.run(
        adapter.send_document(
            "123",
            str(document),
            metadata={"telegram_business_connection_id": "biz-A"},
        )
    )

    assert result.success is True
    assert bot.send_document.await_args.kwargs["business_connection_id"] == "biz-A"


def test_business_turn_skips_draft_api_that_cannot_route_connection():
    adapter = _adapter()
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))
    adapter._bot = bot

    result = asyncio.run(
        adapter.send_draft(
            "123",
            1,
            "working",
            metadata={"telegram_business_connection_id": "biz-A"},
        )
    )

    assert result.success is False
    assert result.error == "business_drafts_unsupported"
    bot.send_message_draft.assert_not_awaited()


class _InputChecklistTask:
    def __init__(self, task_id, text):
        self.id = task_id
        self.text = text


class _InputChecklist:
    def __init__(
        self,
        title,
        tasks,
        *,
        others_can_add_tasks=None,
        others_can_mark_tasks_as_done=None,
    ):
        self.title = title
        self.tasks = tuple(tasks)
        self.others_can_add_tasks = others_can_add_tasks
        self.others_can_mark_tasks_as_done = others_can_mark_tasks_as_done


def _install_checklist_types(monkeypatch):
    monkeypatch.setattr(
        telegram_adapter,
        "_load_input_checklist_types",
        lambda: (_InputChecklist, _InputChecklistTask),
        raising=False,
    )


def test_send_checklist_requires_connection_and_builds_typed_payload(monkeypatch):
    adapter = _adapter()
    checklist_payload = SimpleNamespace(
        title="Launch",
        tasks=("Venue", "Invites"),
        others_can_add_tasks=True,
        others_can_mark_tasks_as_done=False,
    )
    adapter._build_input_checklist = MagicMock(return_value=checklist_payload)
    bot = SimpleNamespace(
        send_checklist=AsyncMock(return_value=SimpleNamespace(message_id=91))
    )
    adapter._bot = bot

    async def _run(cooldown_chat_id, send_fn, *args, **kwargs):
        return await send_fn(*args, **kwargs)

    adapter._run_send_call = _run

    missing = asyncio.run(
        adapter.send_checklist("123", "Launch", [{"id": 1, "text": "Venue"}])
    )
    sent = asyncio.run(
        adapter.send_checklist(
            "123",
            "Launch",
            [{"id": 1, "text": "Venue"}, {"id": 2, "text": "Invites"}],
            business_connection_id="biz-A",
            others_can_add_tasks=True,
            others_can_mark_tasks_as_done=False,
        )
    )

    assert missing.success is False
    assert missing.retryable is False
    assert "business_connection_id" in missing.error
    assert sent.success is True
    assert sent.message_id == "91"
    kwargs = bot.send_checklist.await_args.kwargs
    assert kwargs["business_connection_id"] == "biz-A"
    assert kwargs["checklist"] is checklist_payload
    adapter._build_input_checklist.assert_called_once_with(
        "Launch",
        [{"id": 1, "text": "Venue"}, {"id": 2, "text": "Invites"}],
        others_can_add_tasks=True,
        others_can_mark_tasks_as_done=False,
    )
    assert kwargs["checklist"].others_can_add_tasks is True
    assert kwargs["checklist"].others_can_mark_tasks_as_done is False


@pytest.mark.parametrize(
    ("title", "tasks", "error"),
    [
        ("", [{"id": 1, "text": "Venue"}], "title"),
        ("Launch", [], "1-30"),
        ("Launch", [{"id": 0, "text": "Venue"}], "positive"),
        (
            "Launch",
            [{"id": 1, "text": "Venue"}, {"id": 1, "text": "Invites"}],
            "unique",
        ),
        ("Launch", [{"id": 1, "text": ""}], "1-100"),
    ],
)
def test_input_checklist_validation_is_fail_closed(monkeypatch, title, tasks, error):
    _install_checklist_types(monkeypatch)
    adapter = _adapter()

    with pytest.raises(ValueError, match=error):
        adapter._build_input_checklist(title, tasks)


def test_checklist_validation_rejects_duplicate_ids_before_transport(monkeypatch):
    _install_checklist_types(monkeypatch)
    adapter = _adapter()
    bot = SimpleNamespace(send_checklist=AsyncMock())
    adapter._bot = bot

    result = asyncio.run(
        adapter.send_checklist(
            "123",
            "Launch",
            [{"id": 1, "text": "Venue"}, {"id": 1, "text": "Invites"}],
            business_connection_id="biz-A",
        )
    )

    assert result.success is False
    assert result.retryable is False
    assert "unique" in result.error
    bot.send_checklist.assert_not_awaited()


@pytest.mark.parametrize("reply_to", [True, 0, -1, 1.5, "1.5", ""])
def test_send_checklist_rejects_invalid_reply_message_ids(reply_to):
    adapter = _adapter()
    bot = SimpleNamespace(send_checklist=AsyncMock())
    adapter._bot = bot

    result = asyncio.run(
        adapter.send_checklist(
            "123",
            "Launch",
            [{"id": 1, "text": "Venue"}],
            business_connection_id="biz-A",
            reply_to=reply_to,
        )
    )

    assert result.success is False
    assert result.retryable is False
    assert "positive" in result.error
    bot.send_checklist.assert_not_awaited()


@pytest.mark.parametrize("message_id", [True, 0, -1, 1.5, "1.5", ""])
def test_edit_checklist_rejects_invalid_message_ids(message_id):
    adapter = _adapter()
    bot = SimpleNamespace(edit_message_checklist=AsyncMock())
    adapter._bot = bot

    result = asyncio.run(
        adapter.edit_checklist(
            "123",
            message_id,
            "Launch",
            [{"id": 1, "text": "Venue"}],
            business_connection_id="biz-A",
        )
    )

    assert result.success is False
    assert result.retryable is False
    assert "positive" in result.error
    bot.edit_message_checklist.assert_not_awaited()


def test_edit_checklist_forwards_connection_and_returns_message_id(monkeypatch):
    _install_checklist_types(monkeypatch)
    adapter = _adapter()
    bot = SimpleNamespace(
        edit_message_checklist=AsyncMock(return_value=SimpleNamespace(message_id=92))
    )
    adapter._bot = bot

    result = asyncio.run(
        adapter.edit_checklist(
            "123",
            "77",
            "Launch",
            [{"id": 1, "text": "Venue"}],
            business_connection_id="biz-A",
        )
    )

    assert result.success is True
    assert result.message_id == "92"
    kwargs = bot.edit_message_checklist.await_args.kwargs
    assert kwargs["business_connection_id"] == "biz-A"
    assert kwargs["chat_id"] == 123
    assert kwargs["message_id"] == 77


def test_checklist_transport_retryability_never_authorizes_ambiguous_resend():
    adapter = _adapter()
    transport_error = RuntimeError("temporary network error")

    send_failure = adapter._checklist_transport_failure(
        transport_error, ambiguous_send=True
    )
    edit_failure = adapter._checklist_transport_failure(
        transport_error, ambiguous_send=False
    )
    forbidden_edit = adapter._checklist_transport_failure(
        RuntimeError("Forbidden: bot was blocked"), ambiguous_send=False
    )

    assert send_failure.retryable is False
    assert edit_failure.retryable is True
    assert forbidden_edit.retryable is False
