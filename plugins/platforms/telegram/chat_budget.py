"""One outbound Bot API budget per Telegram chat, shared by every request to that chat.

Telegram meters per CHAT and every Bot API call spends that one allowance: ``sendMessage``,
``editMessageText`` (interim or final), ``sendChatAction``, ``deleteMessage(s)``,
``editForumTopic``, reactions, drafts and media alike. Two limiters that each target the
ceiling sum to twice the ceiling, so this module owns the only per-chat clock and
:class:`ChatBudgetRateLimiter` installs it at PTB's request layer, where raw
``do_api_request`` calls cannot bypass it (``ExtBot._do_post`` routes everything but
``getUpdates`` through ``rate_limiter.process_request``).

Sizing (community envelopes; Telegram publishes none and escalates repeat offences):

========  ===========  =======  ==========  ===========================
class     ceiling/min  target   base gap    typing gap (share of gap)
========  ===========  =======  ==========  ===========================
private   ~60          45/min   1.33s       4.0s (<=15/min of the 45)
group     ~20          15/min   4.0s        12.0s (<=5/min of the 15)
========  ===========  =======  ==========  ===========================

Interim edits and drafts are additionally floored at ``EDIT_FLOOR_SECS`` (3.0s) per chat.
Every metered call takes the same slot, so the per-chat SUM across all paths is bounded by
``60 / base_gap`` per minute, 75% of the class ceiling. A published ``retry_after`` widens
that chat's gap by ``PENALTY_FACTOR`` for ``PENALTY_WINDOW_SECS``, then expires on its own.

Priority: real deliveries wait for their slot (never dropped); cosmetic and superseded
traffic (typing, drafts) is SHED when no slot is free, never queued behind real messages.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from plugins.platforms.telegram.daily_volume import DailyVolume
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

logger = logging.getLogger(__name__)

PRIVATE_CEILING_PER_MIN = 60.0
GROUP_CEILING_PER_MIN = 20.0
HEADROOM = 0.75
PRIVATE_GAP_SECS = 60.0 / (PRIVATE_CEILING_PER_MIN * HEADROOM)
GROUP_GAP_SECS = 60.0 / (GROUP_CEILING_PER_MIN * HEADROOM)
# Typing is a decaying ~5s lease rendered per thread; one chat-wide refresh every 4s keeps a single
# active thread's bubble alive while capping the indicator at a third of the chat's budget.
PRIVATE_TYPING_GAP_SECS = 4.0
GROUP_TYPING_GAP_SECS = 12.0
# An interim edit or draft carries only the latest state, so spacing it costs a reader nothing:
# superseded previews never fire faster than this per chat. Progress producers are tuned to it.
EDIT_FLOOR_SECS = 3.0
PENALTY_FACTOR = 2.0
PENALTY_WINDOW_SECS = 600.0

KIND_DELIVERY = "delivery"  # waits for its slot
KIND_TYPING = "typing"  # shed when no slot, own sub-gap
KIND_INTERIM = "interim"  # shed when no slot (the next tick carries newer state)

_SHED_ENDPOINTS = {
    "sendChatAction": KIND_TYPING,
    "sendMessageDraft": KIND_INTERIM,
    "sendRichMessageDraft": KIND_INTERIM,
}


def chat_key(chat_id: Any) -> str:
    return str(normalize_telegram_chat_id(chat_id))


def is_group_chat(chat_id: Any) -> bool:
    """Groups, supergroups and channels have negative ids and ``@username`` targets are public
    channels/groups, so the id alone selects the class without a ``getChat`` round trip."""
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
    """Per-chat slot clock. Monotonic time so it works from any task or loop.

    ``gap_override`` replaces the class base gap (tests); typing
    is never faster than the base gap, so an override can only tighten the sum it bounds.
    """

    def __init__(self, *, gap_override: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
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
        """Seconds until the chat's next slot is free (0 = a call may fire now)."""
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
        """True when a superseded preview (interim edit, draft) must be skipped right now."""
        state = self._chats.get(chat_key(chat_id))
        if state is None:
            return False
        now = self._clock()
        return now < state.next_at or now < state.interim_next_at

    def note_interim(self, chat_id: Any) -> None:
        """Start the edit floor after an interim edit the adapter is about to fire."""
        key = chat_key(chat_id)
        self._state(key).interim_next_at = self._clock() + self.interim_gap(key)

    def try_take(self, chat_id: Any, kind: str = KIND_INTERIM) -> bool:
        """Take the slot only if it is free right now; the shed path for cosmetic traffic.
        A slot reserved by a waiting delivery keeps ``next_at`` in the future, so shed traffic
        can never jump ahead of a real message."""
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
        """Reserve the next free slot (FIFO) and return how long to wait for it. Callers that are
        cancelled while waiting leave the slot spent, which only errs toward fewer calls."""
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

    def note_retry_after(self, chat_id: Any, wait: float) -> None:
        """A published ``retry_after`` means the chat is already near its limit: widen its gap
        for a bounded window. The server deadline itself is enforced by the durable flood
        store, never slept off here, so a multi-hour penalty cannot park a pacing wait."""
        key = chat_key(chat_id)
        state = self._state(key)
        now = self._clock()
        # The call that published the wait retries on the server's own number; the widened gap
        # applies from the call after it, so the multiplier never inflates the inline retry.
        state.next_at = max(state.next_at, now + min(max(0.0, float(wait)), self.gap(key)))
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


class ChatPenaltyRefusal(Exception):
    """Local refusal inside a known server penalty, for stub ``telegram`` modules without RetryAfter."""

    def __init__(self, retry_after: float):
        self.retry_after = float(retry_after)
        super().__init__(f"flood_control:{self.retry_after}")


def _penalty_refusal(remaining: float) -> Exception:
    """The same ``RetryAfter`` Telegram would return, so every caller's existing classifier applies."""
    try:
        from telegram.error import RetryAfter
        return RetryAfter(_dt.timedelta(seconds=remaining))
    except Exception:
        return ChatPenaltyRefusal(remaining)


def _camel_endpoint(method_name: str) -> str:
    head, *rest = method_name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


class MeteredBot:
    """Wrap a plain ``telegram.Bot`` (or a test double) so each coroutine method passes through a
    :class:`ChatBudgetRateLimiter`, for lanes that do not build an ``ExtBot`` (the standalone sender)."""

    def __init__(self, bot: Any, limiter: "ChatBudgetRateLimiter"):
        self._metered_bot = bot
        self._metered_limiter = limiter

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._metered_bot, name)
        if name.startswith("_") or not asyncio.iscoroutinefunction(attr):
            return attr
        limiter = self._metered_limiter
        endpoint = "doApiRequest" if name == "do_api_request" else _camel_endpoint(name)

        async def metered(*args: Any, **kwargs: Any) -> Any:
            return await limiter.process_request(
                callback=lambda: attr(*args, **kwargs), args=(), kwargs={}, endpoint=endpoint,
                data=kwargs, rate_limit_args=None)

        return metered


class ChatBudgetRateLimiter:
    """PTB request-layer gate: every metered request to a chat spends that chat's budget.

    ``penalty_remaining(key)`` returns the known server penalty (durable flood store); a request
    inside it is refused locally with ``RetryAfter`` and never reaches Telegram, whichever path
    issued it. ``on_retry_after(key, wait)`` persists a newly published penalty.
    """

    def __init__(
        self,
        budget: ChatOutboundBudget,
        *,
        penalty_remaining: Optional[Callable[[str], Optional[float]]] = None,
        on_retry_after: Optional[Callable[[str, float], None]] = None,
        volume: Optional["DailyVolume"] = None,
    ):
        self.budget = budget
        self.volume = volume
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
            if remaining:
                raise _penalty_refusal(float(remaining))
        kind = (rate_limit_args or {}).get("kind") if isinstance(rate_limit_args, dict) else None
        kind = kind or endpoint_kind(endpoint)
        if kind in (KIND_TYPING, KIND_INTERIM):
            if not self.budget.try_take(key, kind):
                self.shed_count += 1
                logger.debug("Telegram chat %s: shed %s (%s), budget slot busy", key, endpoint, kind)
                return True  # sendChatAction and draft endpoints return a bare boolean
        else:
            await self.budget.take(key)
        if self.volume is not None:
            self.volume.record(key, endpoint, data)
        try:
            return await callback(*args, **kwargs)
        except Exception as error:
            wait = _retry_after_seconds(error)
            if wait is not None:
                self.budget.note_retry_after(key, wait)
                if self.volume is not None:
                    self.volume.note_retry_after(key, wait)
                logger.warning("Telegram chat %s: %s refused with retry_after=%.1fs", key, endpoint, wait)
                if self._on_retry_after is not None:
                    try:
                        self._on_retry_after(key, wait)
                    except Exception:
                        logger.warning("Could not record Telegram flood deadline for chat %s", key, exc_info=True)
            raise


try:  # Virtual subclass of PTB's ABC: a real class even when tests stub ``telegram.ext``.
    from telegram.ext import BaseRateLimiter as _BaseRateLimiter
    if isinstance(_BaseRateLimiter, type):
        _BaseRateLimiter.register(ChatBudgetRateLimiter)
except Exception:  # pragma: no cover - installs without telegram.ext cannot run the adapter anyway
    pass
