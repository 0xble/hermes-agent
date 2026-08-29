"""Regression coverage for Telegram native checklist ingress (HERMES-062)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

telegram_adapter = load_plugin_adapter("telegram")
TelegramAdapter = telegram_adapter.TelegramAdapter


def _adapter():
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={"allow_from": ["*"]})
    )
    adapter._is_user_authorized_from_message = lambda message: True
    adapter._should_process_message = lambda message, *, is_command=False: True
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._observe_unmentioned_group_message = MagicMock()
    return adapter


def _task(task_id: int, text: str, *, done: bool = False):
    return SimpleNamespace(
        id=task_id,
        text=text,
        text_entities=[],
        completed_by_user=(
            SimpleNamespace(id=42, full_name="Brian") if done else None
        ),
        completed_by_chat=None,
        completion_date=(
            datetime(2026, 8, 28, tzinfo=timezone.utc) if done else None
        ),
    )


def _checklist(title="Launch", tasks=None, **kwargs):
    return SimpleNamespace(
        title=title,
        title_entities=[],
        tasks=tasks or [],
        others_can_add_tasks=kwargs.get("others_can_add_tasks"),
        others_can_mark_tasks_as_done=kwargs.get("others_can_mark_tasks_as_done"),
    )


def _message(**kwargs):
    defaults = dict(
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
        business_connection_id=None,
        text=None,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        reply_to_message=None,
        quote=None,
        date=datetime.now(timezone.utc),
        checklist=None,
        checklist_tasks_added=None,
        checklist_tasks_done=None,
        forum_topic_created=None,
        api_kwargs={},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _update(message, update_id=900):
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        edited_message=None,
        edited_channel_post=None,
        business_message=None,
        edited_business_message=None,
        effective_message=message,
    )


def test_registration_routes_checklist_before_final_unmatched_guard(monkeypatch):
    class FakeHandler:
        def __init__(self, message_filter, callback):
            self.message_filter = message_filter
            self.callback = callback

    class App:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append((group, handler))

    monkeypatch.setattr(telegram_adapter, "TelegramMessageHandler", FakeHandler)
    adapter = _adapter()
    app = App()

    adapter._register_handlers(app)

    callbacks = [
        handler.callback.__name__
        for group, handler in app.handlers
        if group == 0 and isinstance(handler, FakeHandler)
    ]
    assert callbacks.index("_handle_checklist_message") < callbacks.index(
        "_handle_unmatched_message"
    )
    assert callbacks[-1] == "_handle_unmatched_message"


def test_initial_checklist_dispatches_text_and_structured_metadata():
    adapter = _adapter()
    zero_date_task = _task(3, "Archive notes")
    zero_date_task.completion_date = datetime.fromtimestamp(0, timezone.utc)
    msg = _message(
        checklist=_checklist(
            tasks=[
                _task(1, "Confirm venue", done=True),
                _task(2, "/publish agenda"),
                zero_date_task,
            ],
            others_can_add_tasks=True,
            others_can_mark_tasks_as_done=False,
        )
    )

    asyncio.run(adapter._handle_checklist_message(_update(msg, 901), None))

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == (
        "Checklist: Launch\n"
        "- [x] #1 Confirm venue\n"
        "- [ ] #2 /publish agenda\n"
        "- [ ] #3 Archive notes"
    )
    assert event.allow_gateway_control is False
    assert event.platform_update_id == 901
    assert event.metadata["telegram_checklist"] == {
        "kind": "checklist",
        "title": "Launch",
        "title_entities": [],
        "tasks": [
            {
                "id": 1,
                "text": "Confirm venue",
                "text_entities": [],
                "completed": True,
                "completed_by_user": {"id": "42", "name": "Brian"},
                "completed_by_chat": None,
                "completion_date": "2026-08-28T00:00:00+00:00",
            },
            {
                "id": 2,
                "text": "/publish agenda",
                "text_entities": [],
                "completed": False,
                "completed_by_user": None,
                "completed_by_chat": None,
                "completion_date": None,
            },
            {
                "id": 3,
                "text": "Archive notes",
                "text_entities": [],
                "completed": False,
                "completed_by_user": None,
                "completed_by_chat": None,
                "completion_date": datetime.fromtimestamp(0, timezone.utc).isoformat(),
            },
        ],
        "others_can_add_tasks": True,
        "others_can_mark_tasks_as_done": False,
        "checklist_message_id": "77",
    }


def test_checklist_task_mentions_participate_in_group_gating():
    adapter = _adapter()
    adapter._bot = SimpleNamespace(username="hermes_bot", id=42)
    mention = SimpleNamespace(type="mention", offset=0, length=len("@hermes_bot"))
    task = _task(1, "@hermes_bot review launch")
    task.text_entities = [mention]
    msg = _message(checklist=_checklist(tasks=[task]))

    assert adapter._message_mentions_bot(msg) is True


def test_added_tasks_are_observed_without_triggering_an_unsolicited_turn():
    adapter = _adapter()
    original = _message(checklist=_checklist(tasks=[_task(1, "Venue")]))
    service = _message(
        checklist_tasks_added=SimpleNamespace(
            tasks=[_task(2, "Invite guests")], checklist_message=original
        )
    )

    asyncio.run(adapter._handle_checklist_message(_update(service, 902), None))

    adapter.handle_message.assert_not_awaited()
    adapter._observe_unmentioned_group_message.assert_called_once()
    event = adapter._observe_unmentioned_group_message.call_args.kwargs["event"]
    assert event.text == "Checklist updated: Launch\nAdded:\n- [ ] #2 Invite guests"
    assert event.metadata["telegram_checklist"]["kind"] == "tasks_added"
    assert event.metadata["telegram_checklist"]["checklist_message_id"] == "77"


def test_done_and_undone_ids_are_observed_exactly():
    adapter = _adapter()
    original = _message(
        checklist=_checklist(tasks=[_task(1, "Venue"), _task(2, "Invites")])
    )
    service = _message(
        checklist_tasks_done=SimpleNamespace(
            checklist_message=original,
            marked_as_done_task_ids=[1],
            marked_as_not_done_task_ids=[2],
        )
    )

    asyncio.run(adapter._handle_checklist_message(_update(service, 903), None))

    adapter.handle_message.assert_not_awaited()
    event = adapter._observe_unmentioned_group_message.call_args.kwargs["event"]
    assert event.text == (
        "Checklist updated: Launch\nMarked done: #1\nMarked not done: #2"
    )
    assert event.metadata["telegram_checklist"]["marked_as_done_task_ids"] == [1]
    assert event.metadata["telegram_checklist"]["marked_as_not_done_task_ids"] == [2]


def test_status_without_referenced_checklist_is_still_observed():
    adapter = _adapter()
    service = _message(
        checklist_tasks_done=SimpleNamespace(
            checklist_message=None,
            marked_as_done_task_ids=[7],
            marked_as_not_done_task_ids=[],
        )
    )

    asyncio.run(adapter._handle_checklist_message(_update(service, 904), None))

    adapter.handle_message.assert_not_awaited()
    adapter._observe_unmentioned_group_message.assert_called_once()
    event = adapter._observe_unmentioned_group_message.call_args.kwargs["event"]
    assert event.text == "Checklist updated:\nMarked done: #7"
    assert event.metadata["telegram_checklist"]["marked_as_done_task_ids"] == [7]
    assert event.metadata["telegram_checklist"]["checklist_message_id"] is None


def test_status_update_respects_message_routing_before_observation():
    adapter = _adapter()
    adapter._should_process_message = MagicMock(return_value=False)
    adapter._should_observe_unmentioned_group_message = MagicMock(return_value=False)
    original = _message(checklist=_checklist(tasks=[_task(1, "Venue")]))
    service = _message(
        chat=SimpleNamespace(
            id=-100,
            type="supergroup",
            title="Launch team",
            full_name=None,
            is_forum=False,
        ),
        checklist_tasks_done=SimpleNamespace(
            checklist_message=original,
            marked_as_done_task_ids=[1],
            marked_as_not_done_task_ids=[],
        ),
    )

    asyncio.run(adapter._handle_checklist_message(_update(service, 906), None))

    adapter._should_process_message.assert_called_once_with(service)
    adapter._should_observe_unmentioned_group_message.assert_called_once_with(service)
    adapter._observe_unmentioned_group_message.assert_not_called()
    adapter.handle_message.assert_not_awaited()


def test_edited_business_checklist_is_observed_without_dispatch():
    adapter = _adapter()
    msg = _message(
        business_connection_id="biz-A",
        checklist=_checklist(tasks=[_task(1, "Venue", done=True)]),
    )
    update = _update(msg, 907)
    update.message = None
    update.edited_business_message = msg

    asyncio.run(adapter._handle_checklist_message(update, None))

    adapter.handle_message.assert_not_awaited()
    adapter._observe_unmentioned_group_message.assert_called_once()
    event = adapter._observe_unmentioned_group_message.call_args.kwargs["event"]
    assert event.text == "Checklist edited: Launch\n- [x] #1 Venue"
    assert event.metadata["telegram_checklist"]["kind"] == "checklist_edited"


def test_edited_channel_checklist_is_observed_without_dispatch():
    adapter = _adapter()
    msg = _message(checklist=_checklist(tasks=[_task(1, "Venue", done=True)]))
    update = _update(msg, 912)
    update.message = None
    update.edited_channel_post = msg

    asyncio.run(adapter._handle_checklist_message(update, None))

    adapter.handle_message.assert_not_awaited()
    event = adapter._observe_unmentioned_group_message.call_args.kwargs["event"]
    assert event.metadata["telegram_checklist"]["kind"] == "checklist_edited"


def test_observed_checklist_status_persists_structured_metadata():
    adapter = _adapter()
    transcript_entries = []
    adapter._session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        append_to_transcript=lambda session_id, entry: transcript_entries.append(entry),
    )
    adapter._observe_unmentioned_group_message = TelegramAdapter._observe_unmentioned_group_message.__get__(
        adapter, TelegramAdapter
    )
    original = _message(checklist=_checklist(tasks=[_task(1, "Venue")]))
    service = _message(
        checklist_tasks_done=SimpleNamespace(
            checklist_message=original,
            marked_as_done_task_ids=[1],
            marked_as_not_done_task_ids=[],
        )
    )

    asyncio.run(adapter._handle_checklist_message(_update(service, 908), None))

    assert len(transcript_entries) == 1
    assert transcript_entries[0]["metadata"]["telegram_checklist"] == {
        "kind": "tasks_done",
        "title": "Launch",
        "title_entities": [],
        "tasks": [],
        "others_can_add_tasks": None,
        "others_can_mark_tasks_as_done": None,
        "checklist_message_id": "77",
        "marked_as_done_task_ids": [1],
        "marked_as_not_done_task_ids": [],
    }


def test_malformed_inbound_task_ids_are_bounded_without_crashing():
    adapter = _adapter()
    msg = _message(
        checklist=_checklist(
            tasks=[
                SimpleNamespace(
                    id="not-an-integer",
                    text="Venue",
                    text_entities=[],
                    completed_by_user=None,
                    completed_by_chat=None,
                    completion_date=None,
                )
            ]
        )
    )

    asyncio.run(adapter._handle_checklist_message(_update(msg, 909), None))

    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Checklist: Launch\n- [ ] #? Venue"
    task = event.metadata["telegram_checklist"]["tasks"][0]
    assert task["id"] is None
    assert task["id_invalid"] is True


def test_malformed_done_ids_and_completion_dates_are_not_treated_as_state():
    adapter = _adapter()
    invalid_completion = SimpleNamespace(
        id=1,
        text="Venue",
        text_entities=[],
        completed_by_user=None,
        completed_by_chat=None,
        completion_date="not-a-date",
    )
    initial = _message(checklist=_checklist(tasks=[invalid_completion]))

    asyncio.run(adapter._handle_checklist_message(_update(initial, 910), None))

    initial_event = adapter.handle_message.await_args.args[0]
    task = initial_event.metadata["telegram_checklist"]["tasks"][0]
    assert task["completed"] is False
    assert task["completion_date"] is None
    assert task["completion_date_invalid"] is True

    adapter.handle_message.reset_mock()
    service = _message(
        checklist_tasks_done=SimpleNamespace(
            checklist_message=None,
            marked_as_done_task_ids=["bad", 7],
            marked_as_not_done_task_ids=[False, 8],
        )
    )
    asyncio.run(adapter._handle_checklist_message(_update(service, 911), None))

    event = adapter._observe_unmentioned_group_message.call_args.kwargs["event"]
    metadata = event.metadata["telegram_checklist"]
    assert metadata["marked_as_done_task_ids"] == [7]
    assert metadata["marked_as_not_done_task_ids"] == [8]
    assert metadata["invalid_task_id_counts"] == {"done": 1, "not_done": 1}


def test_unmatched_guard_logs_shape_not_payload(caplog):
    adapter = _adapter()
    secret_payload = "do-not-log-this-payload"

    class _SlottedMessage:
        __slots__ = ("api_kwargs", "chat", "dice", "message_id")

        def __init__(self):
            self.api_kwargs = {"future_payload": secret_payload}
            self.chat = SimpleNamespace(type="private")
            self.dice = SimpleNamespace(value=6)
            self.message_id = 77

        def to_dict(self):
            raise RuntimeError("exercise slots fallback")

    msg = _SlottedMessage()

    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter._handle_unmatched_message(_update(msg, 905), None))

    text = caplog.text
    assert "Unhandled Telegram message-like update" in text
    assert "dice" in text
    assert "future_payload" in text
    assert secret_payload not in text
    adapter.handle_message.assert_not_awaited()
