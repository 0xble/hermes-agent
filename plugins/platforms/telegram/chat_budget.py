"""One outbound Bot API budget per Telegram chat, shared by every request to that chat.

Telegram meters per CHAT and every Bot API call spends that one allowance: ``sendMessage``,
``editMessageText`` (interim or final), ``sendChatAction``, ``deleteMessage(s)``,
``editForumTopic``, reactions, drafts and media alike. Two limiters that each target the
ceiling sum to twice the ceiling, so this module owns the only per-chat clock and
:class:`ChatBudgetRateLimiter` installs it at PTB's request layer, where raw
``do_api_request`` calls cannot bypass it (``ExtBot._do_post`` routes everything but
``getUpdates`` through ``rate_limiter.process_request``).

Sizing uses conservative headroom below Telegram's undocumented class envelopes. Cosmetic
traffic is shed when the next slot is busy; durable traffic waits for its slot. A published
``retry_after`` widens that chat's gap for a bounded window, while optional adapter hooks
can refuse requests during an already-recorded flood cooldown.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

logger = logging.getLogger(__name__)

PRIVATE_CEILING_PER_MIN = 60.0
GROUP_CEILING_PER_MIN = 20.0
HEADROOM = 0.75
PRIVATE_GAP_SECS = 60.0 / (PRIVATE_CEILING_PER_MIN * HEADROOM)
GROUP_GAP_SECS = 60.0 / (GROUP_CEILING_PER_MIN * HEADROOM)
PRIVATE_TYPING_GAP_SECS = 4.0
GROUP_TYPING_GAP_SECS = 12.0
EDIT_FLOOR_SECS = 3.0
PENALTY_FACTOR = 2.0
PENALTY_WINDOW_SECS = 600.0

KIND_DELIVERY = "delivery"
KIND_TYPING = "typing"
KIND_INTERIM = "interim"

_SHED_ENDPOINTS = {
    "sendChatAction": KIND_TYPING,
    "sendMessageDraft": KIND_INTERIM,
    "sendRichMessageDraft": KIND_INTERIM,
}


def chat_key(chat_id: Any) -> str:
    return str(normalize_telegram_chat_id(chat_id))


def is_group_chat(chat_id: Any) -> bool:
    """Groups, supergroups and channels have negative ids; usernames are public targets."""
    normalized = normalize_telegram_chat_id(chat_id)
    if isinstance(normalized, int):
        return normalized < 0
    return str(normalized).startswith("@")


def endpoint_is_metered(endpoint: str) -> bool:
    """Reads (``get*``) do not post into the chat; everything else spends its budget."""
    return not endpoint.startswith("get")


def endpoint_kind(endpoint: str) -> str:
    return _SHED_ENDPOINTS.get(endpoint, KIND_DELIVERY)


@dataclass
class _ChatState:
    next_at: float = 0.0
    typing_next_at: float = 0.0
    interim_next_at: float = 0.0
    penalty_until: float = 0.0


class ChatOutboundBudget:
    """Per-chat slot clock. Monotonic time makes it safe across tasks and event loops."""

    def __init__(self, *, gap_override: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._gap_override = gap_override
        self._clock = clock
        self._chats: Dict[str, _ChatState] = {}

    def _state(self, key: str) -> _ChatState:
        state = self._chats.get(key)
        if state is None:
            state = self._chats[key] = _ChatState()
        return state

    def _penalised(self, state: _ChatState, now: float) -> bool:
        return now < state.penalty_until

    def gap(self, chat_id: Any) -> float:
        key = chat_key(chat_id)
        base = self._gap_override if self._gap_override is not None else (
            GROUP_GAP_SECS if is_group_chat(key) else PRIVATE_GAP_SECS)
        base = max(0.0, float(base))
        state = self._chats.get(key)
        if state is not None and self._penalised(state, self._clock()):
            base *= PENALTY_FACTOR
        return base

    def typing_gap(self, chat_id: Any) -> float:
        floor = GROUP_TYPING_GAP_SECS if is_group_chat(chat_id) else PRIVATE_TYPING_GAP_SECS
        if self._gap_override is not None and self._gap_override <= 0:
            floor = 0.0
        return max(floor, self.gap(chat_id))

    def interim_gap(self, chat_id: Any) -> float:
        floor = EDIT_FLOOR_SECS
        if self._gap_override is not None and self._gap_override <= 0:
            floor = 0.0
        return max(floor, self.gap(chat_id))

    def remaining(self, chat_id: Any) -> float:
        state = self._chats.get(chat_key(chat_id))
        if state is None:
            return 0.0
        return max(0.0, state.next_at - self._clock())

    def typing_blocked(self, chat_id: Any) -> bool:
        state = self._chats.get(chat_key(chat_id))
        if state is None:
            return False
        now = self._clock()
        return now < state.next_at or now < state.typing_next_at

    def interim_blocked(self, chat_id: Any) -> bool:
        state = self._chats.get(chat_key(chat_id))
        if state is None:
            return False
        now = self._clock()
        return now < state.next_at or now < state.interim_next_at

    def note_interim(self, chat_id: Any) -> None:
        key = chat_key(chat_id)
        self._state(key).interim_next_at = self._clock() + self.interim_gap(key)

    def try_take(self, chat_id: Any, kind: str = KIND_INTERIM) -> bool:
        key = chat_key(chat_id)
        state = self._state(key)
        now = self._clock()
        if now < state.next_at:
            return False
        if kind == KIND_TYPING:
            if now < state.typing_next_at:
                return False
            state.typing_next_at = now + self.typing_gap(key)
        elif kind == KIND_INTERIM:
            if now < state.interim_next_at:
                return False
            state.interim_next_at = now + self.interim_gap(key)
        state.next_at = now + self.gap(key)
        return True

    def reserve(self, chat_id: Any) -> float:
        key = chat_key(chat_id)
        state = self._state(key)
        now = self._clock()
        start = max(now, state.next_at)
        state.next_at = start + self.gap(key)
        return start - now

    async def take(self, chat_id: Any) -> None:
        wait = self.reserve(chat_id)
        if wait > 0:
            await asyncio.sleep(wait)

    def note_retry_after(self, chat_id: Any, wait: Any) -> None:
        """Widen the chat's gap after a published retry_after; malformed values are ignored."""
        try:
            wait_f = float(wait)
        except (TypeError, ValueError):
            return
        if wait_f != wait_f or wait_f <= 0.0:
            return
        key = chat_key(chat_id)
        state = self._state(key)
        now = self._clock()
        routine_gap = self._gap_override if self._gap_override is not None else (
            GROUP_GAP_SECS if is_group_chat(key) else PRIVATE_GAP_SECS)
        state.next_at = max(state.next_at, now + min(wait_f, max(0.0, float(routine_gap))))
        state.penalty_until = max(state.penalty_until, now + PENALTY_WINDOW_SECS)


def _retry_after_seconds(error: BaseException) -> Optional[float]:
    value = getattr(error, "retry_after", None)
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _penalty_refusal(remaining: float) -> Exception:
    try:
        from telegram.error import RetryAfter
        return RetryAfter(_dt.timedelta(seconds=remaining))
    except Exception:
        return RuntimeError(f"flood_control:{remaining}")


def _camel_endpoint(method_name: str) -> str:
    head, *rest = method_name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


class MeteredBot:
    """Wrap a plain ``telegram.Bot`` for standalone lanes without an ExtBot limiter."""

    def __init__(self, bot: Any, limiter: "ChatBudgetRateLimiter"):
        self._metered_bot = bot
        self._metered_limiter = limiter

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._metered_bot, name)
        if name.startswith("_") or not asyncio.iscoroutinefunction(attr):
            return attr
        endpoint = "doApiRequest" if name == "do_api_request" else _camel_endpoint(name)

        async def metered(*args: Any, **kwargs: Any) -> Any:
            return await self._metered_limiter.process_request(
                callback=lambda: attr(*args, **kwargs), args=(), kwargs={}, endpoint=endpoint,
                data=kwargs, rate_limit_args=None)

        return metered


class ChatBudgetRateLimiter:
    """PTB request-layer gate for every metered request carrying ``chat_id``."""

    def __init__(self, budget: ChatOutboundBudget, *,
                 penalty_remaining: Optional[Callable[[str], Optional[float]]] = None,
                 on_retry_after: Optional[Callable[[str, float], None]] = None):
        self.budget = budget
        self._penalty_remaining = penalty_remaining
        self._on_retry_after = on_retry_after
        self.shed_count = 0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        chat_id = data.get("chat_id") if isinstance(data, dict) else None
        if chat_id is None or not endpoint_is_metered(endpoint):
            return await callback(*args, **kwargs)
        key = chat_key(chat_id)
        if self._penalty_remaining is not None:
            remaining = self._penalty_remaining(key)
            if remaining is not None and remaining > 0:
                raise _penalty_refusal(float(remaining))
        kind = (rate_limit_args or {}).get("kind") if isinstance(rate_limit_args, dict) else None
        kind = kind or endpoint_kind(endpoint)
        if kind in (KIND_TYPING, KIND_INTERIM):
            if not self.budget.try_take(key, kind):
                self.shed_count += 1
                logger.debug("Telegram chat %s: shed %s (%s), budget slot busy", key, endpoint, kind)
                return True
        else:
            await self.budget.take(key)
        try:
            return await callback(*args, **kwargs)
        except Exception as error:
            wait = _retry_after_seconds(error)
            if wait is not None:
                self.budget.note_retry_after(key, wait)
                logger.warning("Telegram chat %s: %s refused with retry_after=%.1fs", key, endpoint, wait)
                if self._on_retry_after is not None:
                    try:
                        self._on_retry_after(key, wait)
                    except Exception:
                        logger.warning("Could not record Telegram flood cooldown for chat %s", key, exc_info=True)
            raise


try:
    from telegram.ext import BaseRateLimiter as _BaseRateLimiter
    if isinstance(_BaseRateLimiter, type):
        _BaseRateLimiter.register(ChatBudgetRateLimiter)
except Exception:
    pass
