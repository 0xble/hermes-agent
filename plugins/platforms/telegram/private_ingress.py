"""Establish the gateway privacy boundary before Telegram preprocesses an update."""
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from functools import wraps
import logging
from types import SimpleNamespace

from agent.incognito import incognito_scope, is_incognito
from gateway.incognito import event_scope, unattributed_event_scope
from gateway.platforms.event import MessageEvent, MessageType
from .callback_routing import matching_picker_state, picker_callback

logger = logging.getLogger(__name__)

PRIVATE_ATTACHMENT_NOTE = (
    "[Attachment not downloaded: Telegram media requires a disk cache, which is unavailable in incognito. "
    "Your text or caption is still available.]"
)


def _routing_event(adapter, update):
    query = getattr(update, "callback_query", None)
    if not isinstance(getattr(query, "data", None), str):
        query = None
    message = (getattr(query, "message", None) if query else
               (getattr(update, "message", None) or adapter._effective_update_message(update)))
    if message is None:
        return None
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        return None
    user = getattr(query, "from_user", None) if query else getattr(message, "from_user", None)
    chat_type = adapter._chat_type_str(chat)
    chat_type = "group" if chat_type in {"group", "supergroup"} else ("channel" if chat_type == "channel" else "dm")
    source = adapter.build_source(
        chat_id=str(chat_id), chat_type=chat_type,
        user_id=str(user.id) if user else (str(chat_id) if chat_type in {"dm", "channel"} else None),
        thread_id=adapter._effective_message_thread_id(message))
    # Observed group context and addressed non-command turns share a topic-wide session.
    text = "" if query else (getattr(message, "text", None) or getattr(message, "caption", None) or "")
    if (not text.startswith("/") and chat_type == "group"
            and adapter._telegram_observe_unmentioned_group_messages()
            and str(chat_id) in adapter._telegram_observe_allowed_chats()):
        source = adapter._telegram_group_observe_shared_source(source)
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


def incognito_target_refusal(adapter, source):
    """Do not promise privacy when commands and normal turns use different lanes."""
    if (source.chat_type == "group"
            and adapter._telegram_observe_unmentioned_group_messages()
            and str(source.chat_id) in adapter._telegram_observe_allowed_chats()):
        shared_source = adapter._telegram_group_observe_shared_source(source)
        if adapter._source_session_key(source) != adapter._source_session_key(shared_source):
            return (
                "Incognito is unavailable here: commands and observed conversation use different "
                "sessions. Use a DM or a shared topic instead."
            )
    return None


def _callback_session_key(adapter, update):
    """A pending prompt's exact lane also protects stale or cross-user button taps."""
    query = getattr(update, "callback_query", None)
    data = getattr(query, "data", None)
    if not isinstance(data, str):
        return None
    picker = picker_callback(data)
    if picker is not None:
        chat_id = str(getattr(getattr(query, "message", None), "chat_id", ""))
        state = matching_picker_state(adapter, query, chat_id, picker.state_attribute)
        key = state.get("session_key") if state is not None else None
        return key if isinstance(key, str) and key else None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    if parts[0] == "cl":
        key = getattr(adapter, "_clarify_state", {}).get(parts[1])
        if not isinstance(key, str):
            from tools import clarify_gateway
            lookup = getattr(clarify_gateway, "get_session_key", None)
            key = lookup(parts[1]) if callable(lookup) else None
    elif parts[0] == "sc":
        key = getattr(adapter, "_slash_confirm_state", {}).get(parts[2])
    elif parts[0] == "ea":
        try:
            key = getattr(adapter, "_approval_state", {}).get(int(parts[2]))
        except ValueError:
            return None
    else:
        return None
    return key if isinstance(key, str) else None


def _update_routing_scope(adapter, update):
    key = _callback_session_key(adapter, update)
    data = getattr(getattr(update, "callback_query", None), "data", None)
    unmatched_picker = isinstance(data, str) and picker_callback(data) is not None and key is None
    event = None if unmatched_picker else _routing_event(adapter, update)
    if key is not None and event is not None:
        # Keep retry/results stores on the prompt's lane even when another user taps it.
        owner = SimpleNamespace(_session_key_for_source=lambda source: key,
                                session_store=getattr(adapter, "session_store", None))
        return event_scope(owner, event)
    if key is not None:
        return incognito_scope(is_incognito(key), key)
    if event is not None:
        return event_scope(adapter, event)
    # Missing topics or unmatched picker IDs cannot establish a public lane.
    return unattributed_event_scope()


def _refuse_preparation(adapter):
    # PTB may log the raw Update when an exception escapes preparation.
    with incognito_scope(True):
        logger.warning("Telegram privacy routing unavailable; update not dispatched")
    failed = getattr(adapter, "_fail_update_preparation", None)
    if callable(failed):
        failed()


@contextmanager
def telegram_update_scope(adapter, update):
    with ExitStack() as stack:
        try:
            stack.enter_context(_update_routing_scope(adapter, update))
        except Exception:
            _refuse_preparation(adapter)
            yield False
            return
        yield True


@asynccontextmanager
async def async_telegram_update_scope(adapter, update):
    async with AsyncExitStack() as stack:
        try:
            scope = _update_routing_scope(adapter, update)
            if isinstance(scope, AbstractAsyncContextManager):
                await stack.enter_async_context(scope)
            else:
                stack.enter_context(scope)
        except Exception:
            _refuse_preparation(adapter)
            yield False
            return
        yield True


def private_telegram_ingress(fn):
    """Scope preprocessing and direct handler calls using canonical routing."""
    @wraps(fn)
    async def scoped(self, update, *args, **kwargs):
        async with async_telegram_update_scope(self, update) as admitted:
            if admitted:
                return await fn(self, update, *args, **kwargs)
    return scoped


def private_application_class(base):
    """Cover native plugin handlers and PTB error handling before any handler group runs."""
    class PrivateTelegramApplication(base):
        __slots__ = ()

        async def process_update(self, update):
            async with async_telegram_update_scope(self.adapter, update) as admitted:
                if admitted:
                    return await super().process_update(update)

    return PrivateTelegramApplication


def refuse_private_attachment(event) -> bool:
    """Keep captions functional without creating implicit private files or media cache entries."""
    if not is_incognito():
        return False
    event.text = f"{event.text}\n\n{PRIVATE_ATTACHMENT_NOTE}" if event.text else PRIVATE_ATTACHMENT_NOTE
    event.media_urls = []
    event.media_types = []
    event.media_text_inlined = []
    event.message_type = MessageType.TEXT
    return True
