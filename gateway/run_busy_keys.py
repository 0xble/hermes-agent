"""Topical helpers extracted from gateway/run_busy.py."""
from __future__ import annotations
from typing import Optional

def _strip_slot(text: str, slot: str) -> Optional[str]:
    """Remainder after ``slot`` when ``text`` starts with it as a WHOLE slot, else None.

    The ``:``-delimited slot layout is ``build_session_key``'s: an id that merely starts with another
    must never match, so a whole-slot comparison is what every caller here uses. A text that IS the
    slot yields ``""``."""
    from gateway.run_busy import (
        Optional
    )
    if text == slot:
        return ""
    if text.startswith(slot + ":"):
        return text[len(slot) + 1:]
    return None


def _tail_has_slot(tail: str, slot: str) -> bool:
    """True when ``tail``'s FIRST slot is ``slot`` (``tail`` is ``""`` when the key ends at the
    chat id)."""
    return _strip_slot(tail, slot) is not None


def _same_chat_key_slots(
    key: str, *, prefix: str, chat_id: str, scope_id: Optional[str],
) -> Optional[tuple[str, str]]:
    """``(chat_type, tail)`` when ``key`` names the SAME chat as ``prefix`` + ``chat_id``, else None.

    ``prefix`` is the key's fixed-shape head, ``agent:<profile>:<platform>:``. Everything after it is
    matched as TEXT, because ids may themselves contain ``:`` (Matrix ``!room:example.org``).
    ``scope_id`` is Slack's workspace slot — ``build_session_key`` emits it there alone — and a key
    without it still names the same chat; a key carrying a DIFFERENT known scope is another
    workspace's chat. ``tail`` is ``""`` when the key ends at the chat id.
    """
    from gateway.run_busy import (Optional)
    from gateway.run_busy import (
        Optional
    )
    if not key.startswith(prefix):
        return None
    chat_type, _, rem = key[len(prefix):].partition(":")
    candidates = (f"{scope_id}:{chat_id}", chat_id) if scope_id else (chat_id,)
    for candidate in candidates:
        tail = _strip_slot(rem, candidate)
        if tail is not None:
            return chat_type, tail
    return None
