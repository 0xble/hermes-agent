"""Shared ownership of a typing/status surface, including threaded conversations."""

import asyncio
from typing import Any, Optional


def call_typing_with_metadata(method, *args, metadata=None):
    """Preserve legacy adapter overrides without dropping metadata on newer ones."""
    from gateway.platforms.base import BasePlatformAdapter

    if BasePlatformAdapter._accepts_kwarg(method, "metadata", var_kw=True, unknown=False):
        return method(*args, metadata=metadata)
    return method(*args)


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
        task = getattr(self, "_typing_tasks", {}).get(self._typing_scope_key(chat_id, metadata))
        return isinstance(task, asyncio.Task) and not task.done() and not self._typing_is_paused(chat_id, metadata)
