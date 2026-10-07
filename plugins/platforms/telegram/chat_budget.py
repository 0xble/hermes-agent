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

Interim edits and drafts are additionally floored at ``EDIT_FLOOR_SECS`` (10.0s) per chat.
Every metered call takes the same slot, so the per-chat SUM across all paths is bounded by
``60 / base_gap`` per minute, 75% of the class ceiling. A published ``retry_after`` widens
that chat's gap by ``PENALTY_FACTOR`` for ``PENALTY_WINDOW_SECS``, then expires on its own.

Priority: real deliveries wait for their slot (never dropped); cosmetic and superseded
traffic (typing, drafts) is SHED when no slot is free, never queued behind real messages.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

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
# 10s (was 3s, 2026-10-06): progress-bubble edits were the largest share of typed-turn calls on a
# chat that hit Telegram's daily volume ban; Brian approved fewer bubble updates to cut them.
EDIT_FLOOR_SECS = 10.0
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
        counter: Optional[DailyCallCounter] = None,
    ):
        self.budget = budget
        self._penalty_remaining = penalty_remaining
        self._on_retry_after = on_retry_after
        self.counter = counter if counter is not None else call_counter()
        self.shed_count = 0

    def _count(self, key: str, endpoint: str) -> None:
        try:
            self.counter.record(key, endpoint)
        except Exception:  # measurement must never block a send
            logger.debug("Telegram call counter failed for chat %s", key, exc_info=True)

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
        try:
            return await callback(*args, **kwargs)
        except Exception as error:
            wait = _retry_after_seconds(error)
            if wait is not None:
                self.budget.note_retry_after(key, wait)
                logger.warning("Telegram chat %s: %s refused with retry_after=%.1fs (trigger %s)",
                               key, endpoint, wait, current_trigger())
                if wait >= COUNTER_LONG_PENALTY_SECS:
                    # Every chat's totals, not just this one: comparing them at the refusal is what
                    # tells a per-chat volume limit from a per-bot one.
                    try:
                        self.counter.request_summary(None, reason=f"retry_after={wait:.0f}s on chat {key}")
                    except Exception:
                        logger.debug("Telegram call counter summary failed", exc_info=True)
                if self._on_retry_after is not None:
                    try:
                        self._on_retry_after(key, wait)
                    except Exception:
                        logger.warning("Could not record Telegram flood deadline for chat %s", key, exc_info=True)
            raise
        finally:
            # Counted after the request so measurement never shifts pacing. Every call that reached
            # Telegram counts, refused or not; local penalty refusals and shed calls never get here.
            self._count(key, endpoint)


# --- Daily call counter -------------------------------------------------------------------------
# Measurement only: nothing below sheds or delays a call. The 2026-10-04 and 2026-10-05 bans
# (24237s, 34507s) both ended near 09:59 UTC while the chat ran at 0-6 calls/min, well inside the
# per-minute budget above, so the binding limit looks like a volume window. Counting every metered
# call per chat, endpoint and trigger in hourly buckets lets any window (rolling 24h, or one anchored
# at a ban's end) be summed later, and the count at each long retry_after names the threshold.

TRIGGER_UNTAGGED = "untagged"  # cron delivery, outbox replay, standalone lane, adapter housekeeping
TRIGGER_TYPED = "typed"
_TRIGGER: ContextVar[str] = ContextVar("telegram_call_trigger", default=TRIGGER_UNTAGGED)
# First-line prefixes of the gateway's self-injected turns, most specific first.
_TRIGGER_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("[Continuing toward your standing goal", "goal"),
    ("[/loop wakeup", "loop"),
    ("[relay", "relay"),
    ("[IMPORTANT: Background process", "process"),
    ("[ASYNC DELEGATION", "delegation"),
    ("[System note: The previous turn was interrupted", "restart"),
)
_TRIGGER_FRAGMENTS: Tuple[Tuple[str, str], ...] = (
    ("background process", "process"),
    ("background subagent delegation", "delegation"),
    ("background delegation", "delegation"),
)
COUNTER_LOG_INTERVAL_SECS = 3600.0
COUNTER_FLUSH_INTERVAL_SECS = 60.0
COUNTER_RETENTION_SECS = 30 * 86400.0
# A server wait this long is a window penalty, not burst pacing: log the window's counts with it.
COUNTER_LONG_PENALTY_SECS = 600.0
_HOUR = 3600.0
_DAY = 86400.0


def classify_trigger(event: Any) -> str:
    """What caused a turn: a typed message, or one of the gateway's background producers."""
    text = str(getattr(event, "text", "") or "").lstrip()
    head = text[:200]
    for prefix, trigger in _TRIGGER_PREFIXES:
        if head.startswith(prefix):
            return trigger
    if getattr(event, "_heartbeat_session_id", None):
        return "heartbeat"
    if getattr(event, "internal", False):
        lowered = head.lower()
        for fragment, trigger in _TRIGGER_FRAGMENTS:
            if fragment in lowered:
                return trigger
        return "internal"
    return TRIGGER_TYPED


def bind_trigger(event: Any):
    """Label calls made by this event's turn (and tasks it spawns). Returns a reset token."""
    return _TRIGGER.set(classify_trigger(event))


def reset_trigger(token) -> None:
    _TRIGGER.reset(token)


def current_trigger() -> str:
    return _TRIGGER.get()


class DailyCallCounter:
    """Per-chat Bot API call counts by endpoint and trigger, in hourly buckets.

    Counts accumulate in memory, flush to ``telegram-flood-state.db`` in the profile directory at
    most once a minute (additive upserts, so a restart or a second process sharing the profile
    never loses or double-counts a flushed call), and log a rolling 24h summary hourly.
    """

    def __init__(self, profile_dir: Optional[Path] = None, *, wall: Callable[[], float] = time.time,
                 flush_interval: float = COUNTER_FLUSH_INTERVAL_SECS,
                 log_interval: float = COUNTER_LOG_INTERVAL_SECS):
        self._profile_dir = Path(profile_dir) if profile_dir is not None else None
        self._wall = wall
        self._flush_interval = flush_interval
        self._log_interval = log_interval
        self._lock = threading.Lock()
        self._dirty: Dict[Tuple[float, str, str, str], int] = {}
        self._pending_logs: list = []
        self._work = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._busy = 0
        self._db_lock = threading.RLock()  # window() holds it across flush+read
        now = wall()
        self._last_flush = now
        self._last_log = now
        self._ensure_worker()  # started here, never on a send path

    def _home(self) -> Optional[Path]:
        if self._profile_dir is None:
            try:
                from hermes_constants import get_hermes_home
                self._profile_dir = Path(get_hermes_home())
            except Exception:
                return None
        return self._profile_dir

    def record(self, chat: str, endpoint: str, trigger: Optional[str] = None) -> None:
        """Count one call. Never touches disk: persistence and the hourly summary run on the
        counter's own background thread, so a slow or locked database cannot delay a send."""
        now = self._wall()
        bucket = now - (now % _HOUR)
        key = (bucket, chat, endpoint, trigger or current_trigger())
        with self._lock:
            first = not self._dirty
            self._dirty[key] = self._dirty.get(key, 0) + 1
        due_flush = now - self._last_flush >= self._flush_interval
        due_log = now - self._last_log >= self._log_interval
        if due_flush or due_log:
            if due_flush:
                self._last_flush = now
            if due_log:
                self._last_log = now
            self._schedule(log=due_log)
        elif first and self._worker is None:
            self._ensure_worker()  # only if the worker died; a quiet profile persists within one interval

    def _ensure_worker(self) -> None:
        with self._lock:
            worker = self._worker
            if worker is None or not worker.is_alive():
                worker = self._worker = threading.Thread(
                    target=self._run_worker, name="telegram-call-counter", daemon=True)
                worker.start()

    def _schedule(self, *, log: bool = False, chat: Optional[str] = None, reason: str = "hourly") -> None:
        """Hand persistence (and optionally a summary) to the background worker; never blocks."""
        with self._lock:
            if log:
                self._pending_logs.append((chat, reason))
            self._work.set()
        self._ensure_worker()

    def _run_worker(self) -> None:
        # Long-lived: one idle daemon thread per profile, so no send ever pays for a thread start.
        while True:
            if not self._work.wait(timeout=self._flush_interval):
                with self._lock:
                    if not self._dirty and not self._pending_logs:
                        continue
                    # Interval elapsed with counts pending and no explicit wake: flush them anyway.
                    self._work.set()
            with self._lock:
                self._work.clear()
                self._busy += 1
            try:
                self.flush()
                with self._lock:
                    logs, self._pending_logs = self._pending_logs, []
                for chat, reason in logs:
                    self.log_summary(chat, reason=reason)
            except Exception:  # the worker must survive anything a measurement can raise
                logger.debug("Telegram call counter worker failed", exc_info=True)
            finally:
                with self._lock:
                    self._busy -= 1

    def idle(self) -> bool:
        """True when no persistence or summary work is queued or running (tests, shutdown)."""
        with self._lock:
            return not self._busy and not self._work.is_set() and not self._pending_logs

    def request_summary(self, chat: Optional[str] = None, *, reason: str) -> None:
        """Log the window's counts from the worker thread (used at a long ``retry_after``)."""
        self._schedule(log=True, chat=chat, reason=reason)

    def _db(self):
        home = self._home()
        if home is None:
            raise sqlite3.OperationalError("no profile directory for Telegram call counts")
        conn = sqlite3.connect(home / "telegram-flood-state.db", timeout=1.0)
        conn.execute("CREATE TABLE IF NOT EXISTS call_counts (hour REAL NOT NULL, chat_id TEXT NOT NULL, "
                     "endpoint TEXT NOT NULL, trigger TEXT NOT NULL, count INTEGER NOT NULL, "
                     "PRIMARY KEY (hour, chat_id, endpoint, trigger))")
        return closing(conn)

    def flush(self) -> None:
        """Persist pending counts. Blocking: call from the worker thread or tests, never a send path."""
        self._last_flush = self._wall()
        with self._db_lock:
            with self._lock:
                pending, self._dirty = self._dirty, {}
            if not pending or self._home() is None:
                return
            try:
                with self._db() as conn:
                    conn.executemany(
                        "INSERT INTO call_counts VALUES (?,?,?,?,?) ON CONFLICT(hour, chat_id, endpoint, trigger) "
                        "DO UPDATE SET count = count + excluded.count",
                        [(*key, count) for key, count in pending.items()])
                    conn.execute("DELETE FROM call_counts WHERE hour < ?", (self._wall() - COUNTER_RETENTION_SECS,))
                    conn.commit()
            except (OSError, sqlite3.Error):
                logger.debug("Could not persist Telegram call counts; keeping them for the next flush", exc_info=True)
                with self._lock:
                    for key, count in pending.items():
                        self._dirty[key] = self._dirty.get(key, 0) + count

    def window(self, chat: Optional[str] = None, *, since: Optional[float] = None) -> Dict[str, Any]:
        """Totals from ``since`` (default: 24h ago), flushed and unflushed together.

        Counts are hourly buckets, so the window starts at the first WHOLE hour at or after
        ``since`` (it never reaches back before the cutoff). ``since`` in the result is that
        actual start, so a reader sees the exact interval covered."""
        with self._db_lock:  # no concurrent flush can hold counts in flight while we read
            return self._window_locked(chat, since)

    def _window_locked(self, chat: Optional[str], since: Optional[float]) -> Dict[str, Any]:
        self.flush()
        since = self._wall() - _DAY if since is None else since
        floor = since if since % _HOUR == 0 else since - (since % _HOUR) + _HOUR
        rows = []
        if self._home() is not None:
            try:
                with self._db() as conn:
                    query = "SELECT chat_id, endpoint, trigger, SUM(count) FROM call_counts WHERE hour >= ?"
                    params: list = [floor]
                    if chat is not None:
                        query += " AND chat_id = ?"
                        params.append(chat)
                    rows = conn.execute(query + " GROUP BY 1,2,3", params).fetchall()
            except (OSError, sqlite3.Error):
                logger.debug("Could not read Telegram call counts", exc_info=True)
        chats: Dict[str, Dict[str, Any]] = {}
        # Counts the flush could not write (locked or unreadable DB) still belong in the window.
        with self._lock:
            unflushed = [(c, e, t, n) for (hour, c, e, t), n in self._dirty.items()
                         if hour >= floor and (chat is None or c == chat)]
        for chat_id, endpoint, trigger, count in [*rows, *unflushed]:
            entry = chats.setdefault(chat_id, {"total": 0, "endpoints": {}, "triggers": {}})
            entry["total"] += count
            entry["endpoints"][endpoint] = entry["endpoints"].get(endpoint, 0) + count
            entry["triggers"][trigger] = entry["triggers"].get(trigger, 0) + count
        return {"since": floor, "chats": chats, "total": sum(c["total"] for c in chats.values())}

    def log_summary(self, chat: Optional[str] = None, *, reason: str = "hourly") -> Dict[str, Any]:
        summary = self.window(chat)
        since = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(summary["since"]))
        for chat_id, entry in sorted(summary["chats"].items(), key=lambda kv: -kv[1]["total"]):
            logger.info("Telegram chat %s calls since %s (%s): %d total, endpoints %s, triggers %s",
                        chat_id, since, reason, entry["total"], _top(entry["endpoints"]), _top(entry["triggers"]))
        return summary


def _top(counts: Dict[str, int], n: int = 8) -> str:
    return json.dumps(dict(sorted(counts.items(), key=lambda kv: -kv[1])[:n]), separators=(",", ":"))


_COUNTERS: Dict[str, DailyCallCounter] = {}
_COUNTERS_LOCK = threading.Lock()


def call_counter(profile_dir: Optional[Path] = None) -> DailyCallCounter:
    """One counter per profile directory, shared by the gateway bot and the standalone lane.

    ``None`` resolves the active profile NOW, on the caller's context, so the counter is bound to
    a concrete directory before any background worker (which does not inherit the profile
    ContextVar) touches disk. Two profiles in one process therefore never share a counter."""
    if profile_dir is None:
        from hermes_constants import get_hermes_home
        profile_dir = Path(get_hermes_home())
    key = str(Path(profile_dir).resolve())
    with _COUNTERS_LOCK:
        counter = _COUNTERS.get(key)
        if counter is None:
            counter = _COUNTERS[key] = DailyCallCounter(Path(profile_dir))
        return counter


try:  # Virtual subclass of PTB's ABC: a real class even when tests stub ``telegram.ext``.
    from telegram.ext import BaseRateLimiter as _BaseRateLimiter
    if isinstance(_BaseRateLimiter, type):
        _BaseRateLimiter.register(ChatBudgetRateLimiter)
except Exception:  # pragma: no cover - installs without telegram.ext cannot run the adapter anyway
    pass
