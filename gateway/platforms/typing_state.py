"""Shared ownership of a typing/status surface, including threaded conversations."""

import asyncio
from typing import Any, Optional


def call_typing_with_metadata(method, *args, metadata=None):
    """Preserve legacy adapter overrides without dropping metadata on newer ones."""
    from gateway.platforms.base import BasePlatformAdapter

    if BasePlatformAdapter._accepts_kwarg(method, "metadata", var_kw=True, unknown=False):
        return method(*args, metadata=metadata)
    return method(*args)


def typing_metadata_for_source(source, reply_to_message_id=None):
    """Resolve the status surface without changing recipient/send routing.

    Slack can use the triggering message as a synthetic status root even when
    top-level DMs do not create thread sessions. Never pass that root to message
    delivery: flat reply mode must stay flat, including prompts and notices.
    """
    from gateway.platforms.base import _platform_name, _thread_metadata_for_source
    from gateway.session_context import source_route_metadata

    metadata = _thread_metadata_for_source(source, reply_to_message_id)
    if _platform_name(getattr(source, "platform", None)) == "slack":
        metadata = dict(metadata or {})
        if getattr(source, "thread_id", None) is None and reply_to_message_id:
            metadata["thread_id"] = reply_to_message_id
        user_id = getattr(source, "user_id", None)
        if user_id:
            metadata["user_id"] = str(user_id)
        metadata = source_route_metadata(source, metadata)
    return metadata or None


def typing_metadata_for_event(event):
    from gateway.platforms.base import _reply_anchor_for_event

    return typing_metadata_for_source(event.source, _reply_anchor_for_event(event))


def pause_typing_for_prompt(adapter, session_key, chat_id, metadata=None):
    """Remember the turn's refresh surface before an answer can arrive on another route."""
    adapter.__dict__.setdefault("_typing_prompt_surfaces", {})[session_key] = (
        chat_id, dict(metadata) if metadata is not None else None,
    )
    call_typing_with_metadata(adapter.pause_typing_for_chat, chat_id, metadata=metadata)


def resume_typing_after_prompt(adapter, session_key, chat_id, metadata=None):
    surfaces = adapter.__dict__.get("_typing_prompt_surfaces", {})
    chat_id, metadata = surfaces.pop(session_key, (chat_id, metadata))
    call_typing_with_metadata(adapter.resume_typing_for_chat, chat_id, metadata=metadata)


def forget_typing_prompt_surface(adapter, chat_id, metadata=None):
    """Turn teardown owns abandoned prompts, including timeout and cancellation."""
    scope = adapter._typing_scope_key(chat_id, metadata)
    surfaces = adapter.__dict__.get("_typing_prompt_surfaces", {})
    for session_key, (original_chat, original_metadata) in list(surfaces.items()):
        if adapter._typing_scope_key(original_chat, original_metadata) == scope:
            surfaces.pop(session_key, None)


class TypingStateMixin:
    def _typing_scope_key(self, chat_id: str, metadata: Optional[dict[str, Any]] = None):
        """Adapters may refine the surface identity (for example, a workspace)."""
        md = metadata or {}
        thread_id = next((md[k] for k in ("thread_id", "thread_ts", "message_thread_id", "direct_messages_topic_id") if md.get(k) is not None), None)
        return (str(chat_id), str(thread_id)) if thread_id is not None else str(chat_id)

    def set_status_text(self, chat_id: str, text: Optional[str], metadata=None) -> None:
        """Stash a phrase for this surface. Metadata-free callers retain chat-wide defaults."""
        store = self.__dict__.setdefault("_status_text", {})
        key = self._typing_scope_key(chat_id, metadata)
        if text:
            store[key] = text
        else:
            store.pop(key, None)

    def get_status_text(self, chat_id: str, metadata=None) -> Optional[str]:
        store = getattr(self, "_status_text", {})
        return store.get(self._typing_scope_key(chat_id, metadata)) or store.get(str(chat_id))

    def pause_typing_for_chat(self, chat_id: str, metadata=None) -> None:
        """Pause one surface, or the whole chat when no thread metadata is supplied."""
        self.__dict__.setdefault("_typing_paused", set()).add(self._typing_scope_key(chat_id, metadata))

    def resume_typing_for_chat(self, chat_id: str, metadata=None) -> None:
        paused = getattr(self, "_typing_paused", None)
        if paused is not None:
            paused.discard(self._typing_scope_key(chat_id, metadata))

    def _typing_is_paused(self, chat_id: str, metadata=None) -> bool:
        paused = getattr(self, "_typing_paused", ())
        return str(chat_id) in paused or self._typing_scope_key(chat_id, metadata) in paused

    def _typing_is_active(self, chat_id: str, metadata=None) -> bool:
        task = getattr(self, "_typing_refresh_owners", {}).get(self._typing_scope_key(chat_id, metadata))
        return isinstance(task, asyncio.Task) and not task.done() and not self._typing_is_paused(chat_id, metadata)
