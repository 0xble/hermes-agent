"""Telegram preprocessing must enter the same privacy scope as gateway admission."""
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.incognito import is_incognito, register_session
from gateway.config import PlatformConfig
from gateway.incognito import private_topic
from gateway.platforms.event import MessageType
from plugins.platforms.telegram import adapter as telegram
from plugins.platforms.telegram.private_ingress import PRIVATE_ATTACHMENT_NOTE


def message(**changes):
    values = dict(
        message_id=42, text=None, caption=None, date=datetime.now(UTC),
        chat=SimpleNamespace(id=100, type="private", title=None, full_name="Tester", is_forum=False),
        from_user=SimpleNamespace(id=1, full_name="Tester", first_name="Tester", is_bot=False),
        message_thread_id=7, is_topic_message=True, reply_to_message=None, quote=None,
        entities=[], caption_entities=[], api_kwargs={}, forum_topic_created=None,
        photo=None, video=None, audio=None, voice=None, document=None, sticker=None,
        media_group_id=None, chat_id=100,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def update(msg=None, query=None):
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=query, update_id=12)


@pytest.fixture
def adapter(monkeypatch):
    instance = telegram.TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(instance, "_is_callback_user_authorized", lambda *args, **kwargs: True)
    monkeypatch.setattr(instance, "_is_user_authorized_from_message", lambda msg: True)
    instance._ensure_forum_commands = AsyncMock()
    if telegram.ParseMode is None:
        monkeypatch.setattr(telegram, "ParseMode", SimpleNamespace(HTML="HTML"))
    return instance


def key_for(adapter, msg):
    event = adapter._build_message_event(msg, MessageType.TEXT)
    event = adapter._apply_telegram_group_observe_attribution(event)
    return adapter._source_session_key(event.source)


@pytest.fixture
def private_lane(adapter):
    key = key_for(adapter, message())
    register_session(key)
    yield key
    register_session(key, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["photo", "voice", "audio", "video", "document", "sticker"])
async def test_private_media_refused_before_download_preserves_caption(adapter, private_lane, kind, tmp_path):
    attachment = SimpleNamespace(get_file=AsyncMock(), file_size=100, file_name="private.txt", mime_type="text/plain")
    msg = message(caption="PRIVATE_CAPTION", **{kind: [attachment] if kind == "photo" else attachment})
    seen = []

    async def receive(event):
        seen.append((event, is_incognito(), private_topic()))

    adapter.handle_message = AsyncMock(side_effect=receive)
    await adapter._handle_media_message(update(msg), None)
    attachment.get_file.assert_not_awaited()
    event, private, topic = seen[0]
    assert private and topic == private_lane
    assert event.text == "PRIVATE_CAPTION\n\n" + PRIVATE_ATTACHMENT_NOTE
    assert event.message_type == MessageType.TEXT
    assert not event.media_urls and not event.media_types
    assert not is_incognito()


@pytest.mark.asyncio
async def test_sidecar_scopes_preprocessing_after_restart(adapter, monkeypatch, tmp_path):
    msg = message(photo=[SimpleNamespace(get_file=AsyncMock())], caption="PRIVATE_RESTART_CAPTION")
    key = key_for(adapter, msg)
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "incognito.json").write_text(json.dumps({key: {"session_id": "private-restart-session"}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import get_hermes_home
    get_hermes_home()  # First-use static defaults are unrelated to inbound content.
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    adapter.handle_message = AsyncMock()
    try:
        await adapter._handle_media_message(update(msg), None)
        msg.photo[0].get_file.assert_not_awaited()
        assert PRIVATE_ATTACHMENT_NOTE in adapter.handle_message.call_args.args[0].text
        after = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
        assert all(after[path] == content for path, content in before.items())
        assert after.keys() - before.keys() <= {Path("SOUL.md")}
        assert all(b"PRIVATE_RESTART_CAPTION" not in content for content in after.values())
    finally:
        register_session(key, False)
        register_session("private-restart-session", False)


@pytest.mark.asyncio
async def test_private_reply_media_does_not_break_text_chat(adapter, private_lane):
    photo = SimpleNamespace(get_file=AsyncMock())
    msg = message(text="PRIVATE_TEXT", reply_to_message=message(photo=[photo]))
    seen = []

    async def receive(event):
        seen.append((event, is_incognito(), private_topic()))

    adapter._prepare_text_event = AsyncMock(side_effect=receive)
    await adapter._handle_text_message(update(msg), None)
    photo.get_file.assert_not_awaited()
    event, private, topic = seen[0]
    assert private and topic == private_lane
    assert "PRIVATE_TEXT" in event.text and PRIVATE_ATTACHMENT_NOTE in event.text
    assert not event.media_urls


@pytest.mark.asyncio
async def test_private_entry_command_preprocessing_is_scoped(adapter):
    msg = message(text="/incognito")
    seen = []

    async def receive(event):
        seen.append(is_incognito())

    adapter.handle_message = AsyncMock(side_effect=receive)
    await adapter._handle_command(update(msg), None)
    assert seen == [True]
    assert not is_incognito()


@pytest.mark.asyncio
async def test_public_photo_uses_existing_cache_and_batch_route(adapter, monkeypatch):
    file_obj = SimpleNamespace(file_path="photo.jpg", download_as_bytearray=AsyncMock(return_value=bytearray(b"image")))
    photo = SimpleNamespace(get_file=AsyncMock(return_value=file_obj))
    cache = AsyncMock(return_value="/public/image.jpg")
    monkeypatch.setattr(telegram, "cache_image_from_bytes_async", cache)
    adapter._route_photo_event = AsyncMock()
    await adapter._handle_media_message(update(message(photo=[photo], caption="PUBLIC_CAPTION")), None)
    cache.assert_awaited_once_with(b"image", ext=".jpg")
    event = adapter._route_photo_event.call_args.args[1]
    assert event.media_urls == ["/public/image.jpg"]
    assert event.text == "PUBLIC_CAPTION" and event.message_type == MessageType.PHOTO


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["0", "other"])
@pytest.mark.parametrize("private", [True, False])
async def test_clarify_callbacks_keep_answers_functional_without_private_logs(
    adapter, monkeypatch, tmp_path, choice, private,
):
    from tools import clarify_gateway as clarify
    msg = message(text="PRIVATE_QUESTION")
    key = key_for(adapter, msg)
    clarify_id = f"privacy-{choice}-{private}"
    clarify.register(clarify_id, key, "PRIVATE_QUESTION", ["PRIVATE_CHOICE"])
    adapter._clarify_state[clarify_id] = key
    query = SimpleNamespace(
        data=f"cl:{clarify_id}:{choice}", message=msg, from_user=msg.from_user,
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    delivery_scopes = []

    async def edit(**kwargs):
        delivery_scopes.append((is_incognito(), private_topic()))
        logging.getLogger("telegram-privacy-test").warning("PRIVATE_EDIT %s", kwargs["text"])

    query.edit_message_text.side_effect = edit
    path = tmp_path / "diagnostics.log"
    handler = logging.FileHandler(path)
    monkeypatch.setattr(telegram.logger, "level", logging.INFO)
    logging.getLogger().addHandler(handler)
    register_session(key, private)
    try:
        await adapter._handle_callback_query(update(query=query), None)
        entry = clarify._entries[clarify_id]
        if choice == "0":
            assert entry.response == "PRIVATE_CHOICE" and entry.event.is_set()
            assert "PRIVATE_CHOICE" in query.answer.call_args.kwargs["text"]
        else:
            assert entry.awaiting_text and not entry.event.is_set()
            assert clarify_id in adapter._clarify_state
        assert delivery_scopes == [(private, key)]
        handler.flush()
        logs = path.read_text()
        if private:
            assert "PRIVATE_" not in logs
            assert "Incognito: diagnostic content suppressed" in logs
        else:
            assert "PRIVATE_QUESTION" in logs
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
        register_session(key, False)
        with clarify._lock:
            clarify._entries.pop(clarify_id, None)
            clarify._session_index.pop(key, None)
    assert not is_incognito()


@pytest.mark.asyncio
async def test_observed_group_attachments_use_shared_private_lane(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_telegram_observe_unmentioned_group_messages", lambda: True)
    monkeypatch.setattr(adapter, "_telegram_observe_allowed_chats", lambda: {"100"})
    monkeypatch.setattr(adapter, "_should_process_message", lambda msg: False)
    monkeypatch.setattr(adapter, "_should_observe_unmentioned_group_message", lambda msg: True)
    photo = SimpleNamespace(get_file=AsyncMock())
    msg = message(chat=SimpleNamespace(id=100, type="supergroup", title="Group", is_forum=True), photo=[photo])
    key = key_for(adapter, msg)
    register_session(key)
    seen = []

    def observe(*args, event, **kwargs):
        seen.append((is_incognito(), private_topic(), event))

    monkeypatch.setattr(adapter, "_observe_unmentioned_group_message", observe)
    try:
        await adapter._handle_media_message(update(msg), None)
        photo.get_file.assert_not_awaited()
        private, topic, event = seen[0]
        assert private and topic == key
        assert PRIVATE_ATTACHMENT_NOTE in event.text
        assert not event.media_urls
    finally:
        register_session(key, False)


@pytest.mark.parametrize("thread_id,refused", [(None, True), ("99", False)])
def test_observed_conversation_refuses_only_mismatched_command_lane(adapter, monkeypatch, thread_id, refused):
    monkeypatch.setattr(adapter, "_telegram_observe_unmentioned_group_messages", lambda: True)
    monkeypatch.setattr(adapter, "_telegram_observe_allowed_chats", lambda: {"100"})
    source = adapter.build_source(chat_id="100", chat_type="group", user_id="2", thread_id=thread_id)
    assert bool(adapter.incognito_target_refusal(source)) is refused
    assert source.user_id == "2"
    dm = adapter.build_source(chat_id="100", chat_type="dm", user_id="2", thread_id=thread_id)
    assert adapter.incognito_target_refusal(dm) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["handler", "application"])
@pytest.mark.parametrize("private", [False, True])
async def test_native_async_ingress_resolves_guarded_store_off_loop(adapter, boundary, private):
    import asyncio
    import threading

    from hermes_loop_guard import LoopGuardedLock, set_loop_thread
    from plugins.platforms.telegram.private_ingress import private_application_class

    key = key_for(adapter, message())
    lock = LoopGuardedLock("session-store lock")
    seen = []
    lookup_threads = []

    def enabled(session_key):
        with lock:
            lookup_threads.append(threading.get_ident())
            assert session_key == key
            return private

    adapter.session_store = SimpleNamespace(incognito_enabled=enabled)

    async def receive(*args):
        await asyncio.sleep(0)
        seen.append((threading.get_ident(), is_incognito(), private_topic()))
        assert await asyncio.to_thread(is_incognito) is private

    class Application:
        def __init__(self):
            self.adapter = adapter

        async def process_update(self, incoming):
            await receive(incoming)

    loop_thread = threading.get_ident()
    set_loop_thread(loop_thread)
    try:
        incoming = update(message(text="FIXTURE_INGRESS_TEXT"))
        if boundary == "handler":
            adapter._prepare_text_event = AsyncMock(side_effect=receive)
            await adapter._handle_text_message(incoming, None)
        else:
            await private_application_class(Application)().process_update(incoming)
        assert seen == [(loop_thread, private, key)]
        assert lookup_threads and all(thread != loop_thread for thread in lookup_threads)
        assert not is_incognito() and private_topic() is None
    finally:
        set_loop_thread(None)


@pytest.mark.asyncio
async def test_application_boundary_scopes_native_plugins_tasks_and_errors(adapter, private_lane, tmp_path):
    import asyncio
    from plugins.platforms.telegram.private_ingress import private_application_class

    seen = []

    class Application:
        def __init__(self):
            self.adapter = adapter

        async def process_update(self, incoming):
            seen.append(("handler", is_incognito(), private_topic()))

            async def background():
                seen.append(("task", is_incognito(), private_topic()))

            await asyncio.create_task(background())
            try:
                raise RuntimeError("PRIVATE_SDK_EXCEPTION")
            except RuntimeError:
                logging.getLogger("telegram-privacy-test").exception("PRIVATE_PLUGIN_ERROR")

    path = tmp_path / "sdk.log"
    handler = logging.FileHandler(path)
    logging.getLogger().addHandler(handler)
    try:
        await private_application_class(Application)().process_update(update(message(text="PRIVATE_NATIVE_TEXT")))
        handler.flush()
        assert seen == [("handler", True, private_lane), ("task", True, private_lane)]
        assert "PRIVATE_" not in path.read_text()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert not is_incognito()


@pytest.mark.asyncio
async def test_callback_exact_prompt_lane_wins_over_tapping_user_lane(adapter, private_lane):
    from plugins.platforms.telegram.private_ingress import telegram_update_scope

    adapter._clarify_state["prompt"] = private_lane
    msg = message(message_thread_id=99)
    query = SimpleNamespace(data="cl:prompt:0", message=msg, from_user=SimpleNamespace(id=2))
    assert key_for(adapter, msg) != private_lane
    with telegram_update_scope(adapter, update(query=query)):
        assert is_incognito() and private_topic() == private_lane
    assert not is_incognito()


@pytest.mark.asyncio
async def test_edited_message_observer_inherits_private_scope(adapter, private_lane, monkeypatch):
    from hermes_cli import lifecycle

    msg = message(text="PRIVATE_EDIT_TEXT", edit_date=datetime.now(UTC))
    incoming = update()
    incoming.edited_message = msg
    incoming.effective_message = msg
    seen = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda hook: True)

    async def receive(event, source):
        seen.append((is_incognito(), private_topic(), event["payload"]["text"]))

    adapter._platform_event_handler = receive
    await adapter._on_platform_update(incoming, None)
    assert seen == [(True, private_lane, "PRIVATE_EDIT_TEXT")]


@pytest.mark.parametrize("kind", ["reaction", "inline"])
@pytest.mark.parametrize("private", [False, True])
def test_unattributed_updates_never_guess_a_public_topic(adapter, kind, private):
    from plugins.platforms.telegram.private_ingress import telegram_update_scope

    key = key_for(adapter, message())
    incoming = update()
    if kind == "reaction":
        incoming.message_reaction = SimpleNamespace(chat=message().chat, message_id=42)
    else:
        incoming.inline_query = SimpleNamespace(query="TEST_PRIVATE_INLINE", from_user=message().from_user)
    register_session(key, private)
    try:
        with telegram_update_scope(adapter, incoming) as admitted:
            assert admitted and is_incognito() is private
            assert private_topic() is None
    finally:
        register_session(key, False)
    assert not is_incognito()


def test_unattributed_update_reads_bom_sidecar_before_first_routed_message(adapter, monkeypatch, tmp_path):
    from plugins.platforms.telegram.private_ingress import telegram_update_scope

    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    path = home / "sessions" / "incognito.json"
    path.write_text(json.dumps({"private-route": {"session_id": "private-session"}}), encoding="utf-8-sig")
    monkeypatch.setenv("HERMES_HOME", str(home))
    original = path.read_bytes()
    with telegram_update_scope(adapter, update()) as admitted:
        assert admitted and is_incognito() and private_topic() is None
    assert path.read_bytes() == original
    assert not is_incognito()


def test_known_public_topic_stays_public_while_another_topic_is_private(adapter, private_lane):
    from plugins.platforms.telegram.private_ingress import telegram_update_scope

    msg = message(text="PUBLIC_TOPIC_TEXT", message_thread_id=99)
    public_key = key_for(adapter, msg)
    assert public_key != private_lane
    with telegram_update_scope(adapter, update(msg)) as admitted:
        assert admitted and not is_incognito() and private_topic() == public_key
    assert not is_incognito()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["handler", "application"])
async def test_privacy_preparation_failure_is_content_free_and_never_dispatches(
    adapter, monkeypatch, tmp_path, caplog, boundary,
):
    from plugins.platforms.telegram.private_ingress import private_application_class

    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "incognito.json").write_text("TEST_PRIVATE_MALFORMED_METADATA")
    monkeypatch.setenv("HERMES_HOME", str(home))
    dispatched = AsyncMock()
    incoming = update(message(text="TEST_PRIVATE_UNDISPATCHED_CONTENT"))
    if boundary == "handler":
        adapter._prepare_text_event = dispatched
        await adapter._handle_text_message(incoming, None)
    else:
        class Application:
            def __init__(self):
                self.adapter = adapter

            async def process_update(self, update):
                await dispatched(update)

        await private_application_class(Application)().process_update(incoming)
    dispatched.assert_not_awaited()
    assert "TEST_PRIVATE_" not in caplog.text
    assert not is_incognito()


@pytest.mark.asyncio
async def test_private_update_receipt_is_not_saved_by_a_later_public_flush(adapter, tmp_path):
    from agent.incognito import incognito_scope
    from plugins.platforms.telegram.update_admission import _record_receipt

    adapter._update_receipt_dir = tmp_path / "receipts"
    with incognito_scope(True):
        _record_receipt(adapter, "123:10")
    assert "123:10" in adapter._seen_update_ids
    assert adapter._seen_update_ids["123:10"] is None
    assert adapter._update_receipt_flush is None
    _record_receipt(adapter, "123:11")
    flush = adapter._update_receipt_flush
    assert flush is not None
    await flush
    payload = json.loads((adapter._update_receipt_dir / "telegram_update_receipts_123.json").read_text(encoding="utf-8-sig"))
    assert set(payload["update_ids"]) == {"11"}
    assert "123:10" in adapter._seen_update_ids


def picker_query(adapter, kind, session_key, *, prompt_id=42, callback_id=42, chat_id=100):
    callback = AsyncMock(return_value="Selected")
    state = {
        "msg_id": prompt_id, "session_key": session_key,
        "choices": [{"value": "enabled"}], "on_choice_selected": callback,
        "model_list": ["fixture-model"], "on_model_selected": callback,
        "selected_provider": "fixture-provider", "providers": [],
    }
    getattr(adapter, f"_{kind}_picker_state")["100"] = state
    msg = message(message_id=callback_id, message_thread_id=99, chat_id=chat_id)
    msg.chat.id = chat_id
    query = SimpleNamespace(
        data="cp:0" if kind == "choice" else "mc:0", message=msg,
        from_user=SimpleNamespace(id=2, first_name="Tester"),
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    return query, state, callback


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["model", "choice"])
@pytest.mark.parametrize("private", [False, True])
async def test_picker_selection_uses_only_the_exact_prompt_lane(adapter, kind, private):
    key = key_for(adapter, message())
    query, _state, callback = picker_query(adapter, kind, key)
    seen = []

    async def select(*args):
        seen.append((is_incognito(), private_topic()))
        return "Selected"

    callback.side_effect = select
    register_session(key, private)
    try:
        await adapter._handle_callback_query(update(query=query), None)
        callback.assert_awaited_once()
        assert seen == [(private, key)]
    finally:
        register_session(key, False)
    assert not is_incognito()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["model", "choice"])
@pytest.mark.parametrize("mismatch", ["message", "chat"])
async def test_stale_picker_never_selects_or_consumes_a_newer_prompt(adapter, private_lane, kind, mismatch):
    query, state, callback = picker_query(
        adapter, kind, private_lane,
        prompt_id=43 if mismatch == "message" else 42,
        chat_id=200 if mismatch == "chat" else 100,
    )
    await adapter._handle_callback_query(update(query=query), None)
    callback.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert getattr(adapter, f"_{kind}_picker_state")["100"] is state
    assert not is_incognito()


@pytest.mark.parametrize("data", ["mp:p", "mpg:g", "mpv:0", "mm:0", "mc:0", "mb", "mx", "mx:noop", "mg:0", "cp:0"])
def test_every_picker_callback_family_resolves_the_matching_prompt_lane(adapter, private_lane, data):
    from plugins.platforms.telegram.private_ingress import telegram_update_scope

    kind = "choice" if data.startswith("cp:") else "model"
    query, _state, _callback = picker_query(adapter, kind, private_lane)
    query.data = data
    with telegram_update_scope(adapter, update(query=query)) as admitted:
        assert admitted and is_incognito() and private_topic() == private_lane
    assert not is_incognito()


@pytest.mark.parametrize("kind", ["model", "choice"])
def test_unmatched_picker_does_not_borrow_the_latest_prompt_lane(adapter, private_lane, kind):
    from plugins.platforms.telegram.private_ingress import telegram_update_scope

    query, _state, callback = picker_query(adapter, kind, private_lane, prompt_id=43)
    with telegram_update_scope(adapter, update(query=query)) as admitted:
        assert admitted and is_incognito()
        assert private_topic() is None
    callback.assert_not_awaited()
    assert not is_incognito()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["model", "choice"])
async def test_exact_public_picker_ignores_an_unrelated_private_received_topic(adapter, private_lane, kind):
    public_key = key_for(adapter, message(message_thread_id=99))
    assert public_key != private_lane
    query, _state, callback = picker_query(adapter, kind, public_key)
    query.message.message_thread_id = 7
    query.from_user = query.message.from_user
    assert key_for(adapter, query.message) == private_lane
    seen = []

    async def select(*args):
        seen.append((is_incognito(), private_topic()))
        return "Selected"

    callback.side_effect = select
    await adapter._handle_callback_query(update(query=query), None)
    callback.assert_awaited_once()
    assert seen == [(False, public_key)]
    assert not is_incognito()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["model", "choice"])
@pytest.mark.parametrize("callback_id", [None, True, "42"])
async def test_picker_without_a_real_message_id_is_unattributed_and_cannot_select(
    adapter, private_lane, kind, callback_id,
):
    from plugins.platforms.telegram.private_ingress import telegram_update_scope

    query, state, callback = picker_query(adapter, kind, private_lane, callback_id=callback_id)
    with telegram_update_scope(adapter, update(query=query)) as admitted:
        assert admitted and is_incognito() and private_topic() is None
    await adapter._handle_callback_query(update(query=query), None)
    callback.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert getattr(adapter, f"_{kind}_picker_state")["100"] is state
    assert not is_incognito()

