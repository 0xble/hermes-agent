"""Per-chat daily volume ledger and budget for Telegram Bot API calls.

The per-minute budget in ``chat_budget.py`` did not prevent multi-hour bans: on 2026-10-04 and
2026-10-05 the DM was refused for 24237s and 34507s while traffic was 0-6 sends/min. Every long
ban since 2026-09-28 has ended at a fixed time of day (09:33:23-24 UTC on two consecutive days,
09:58:45 and 09:58:59 on the next pair). Both measured windows reached ~1930 in-turn sends before
the refusal, while days with up to ~1100 were never banned. That reads as a daily volume cap that
Telegram does not document, rather than a rate limit.

This module counts every metered call per chat and per window, by endpoint. It persists the counts
in the profile's flood store so a restart does not zero the day, and logs them hourly. A published
``retry_after`` of an hour or more re-anchors the window at the moment Telegram named (the reset),
and logs the counts at the refusal so the next incident can be compared against them directly.

Shedding follows the message count against ``soft_ceiling`` (``PlatformConfig.extra``
``daily_message_soft_ceiling``):
- At ``COSMETIC_FRACTION`` of the ceiling, typing, interim edits, drafts and progress bubbles are
  shed.
- At the ceiling, non-final notices are also shed.
- Final replies are never shed.

Messages alone did not bound the day. The 2026-10-24 bans (2967s at 02:17 PDT, then 36261s at
17:03 PDT) came after 26174 and 19875 calls while message counts sat at 3024 and 2407, below
ban-free days with 3112 and 4157. Edits and typing created no messages, so background producers spent
~9k calls overnight before the message rule engaged, and typed turns were never metered at all
(10116 of the 19875). ``call_soft_ceiling`` (``daily_call_soft_ceiling``) bounds every metered call:
- Background cosmetic traffic is shed at ``BACKGROUND_CALL_FRACTION`` of it.
- At the ceiling, typed turns shed typing, interim edits and drafts, and background notices
  are shed. The initial tool-progress bubble, typed notices and every final still go out.
- For ``STRICT_WINDOW_SECS`` after a penalty of ``STRICT_PENALTY_SECS`` or more, the call ceiling
  is scaled by ``STRICT_FACTOR``: the 36261s ban arrived at fewer calls than the ban before it,
  15 hours later, so a recent ban lowers the safe volume.

Cleanup deletes are never shed: they create no message, so shedding them saves nothing against the
daily cap and only strands the bubbles they would remove. The per-minute budget still meters them.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DAY_SECS = 86400.0
DEFAULT_SOFT_CEILING = 1500
COSMETIC_FRACTION = 0.7
# Lowest long-ban window measured was 19875 calls (right after an earlier ban); unpenalized long
# bans came at 24296 and 26174. 12000 keeps a normal day near half of those.
DEFAULT_CALL_SOFT_CEILING = 12000
BACKGROUND_CALL_FRACTION = 0.5
STRICT_PENALTY_SECS = 600.0
STRICT_WINDOW_SECS = 86400.0
STRICT_FACTOR = 0.5
TRIGGER_UNTAGGED = "untagged"  # chat_budget.TRIGGER_UNTAGGED; that module imports this one
LOG_INTERVAL_SECS = 3600.0
FLUSH_INTERVAL_SECS = 60.0
# A server penalty this long is a window reset, not a burst penalty, so it re-anchors the window.
RESET_PENALTY_SECS = 3600.0

PRESSURE_NONE = 0
PRESSURE_COSMETIC = 1
PRESSURE_NOTICES = 2

from gateway.platforms.base import (  # noqa: E402
    OUTBOUND_FINAL, OUTBOUND_NOTICE, OUTBOUND_PROGRESS, OUTBOUND_TOOL_PROGRESS, current_outbound_class, outbound_class)

# Endpoints that create a message in the chat. A media group creates one message per item.
_MESSAGE_ENDPOINTS = frozenset({
    "sendMessage", "sendRichMessage", "sendPhoto", "sendDocument", "sendVideo", "sendVoice", "sendAudio",
    "sendAnimation", "sendSticker", "sendVideoNote", "sendLocation", "sendVenue", "sendContact", "sendPoll",
    "sendDice", "sendMediaGroup", "copyMessage", "forwardMessage", "copyMessages", "forwardMessages",
})

def messages_created(endpoint: str, data: Any) -> int:
    if endpoint not in _MESSAGE_ENDPOINTS:
        return 0
    if endpoint == "sendMediaGroup" and isinstance(data, dict):
        media = data.get("media")
        if isinstance(media, (list, tuple)) and media:
            return len(media)
    if endpoint in ("copyMessages", "forwardMessages") and isinstance(data, dict):
        ids = data.get("message_ids")
        if isinstance(ids, (list, tuple)) and ids:
            return len(ids)
    return 1


@dataclass
class _Window:
    start: float
    calls: Dict[str, int] = field(default_factory=dict)
    messages: int = 0
    dirty: Dict[str, int] = field(default_factory=dict)
    dirty_messages: int = 0


class DailyQuota:
    """Per-chat call ledger over a rolling 24h window anchored at the last observed reset.

    The anchor defaults to UTC midnight. ``wall`` is injectable for tests.
    """

    def __init__(self, *, soft_ceiling: Optional[int] = DEFAULT_SOFT_CEILING, profile_dir: Optional[Path] = None,
                 wall: Callable[[], float] = time.time, flush_interval: float = FLUSH_INTERVAL_SECS,
                 call_soft_ceiling: Optional[int] = DEFAULT_CALL_SOFT_CEILING):
        self.soft_ceiling = soft_ceiling
        self.call_soft_ceiling = call_soft_ceiling
        self._profile_dir = Path(profile_dir) if profile_dir is not None else None
        self._wall = wall
        self._flush_interval = flush_interval
        self._windows: Dict[str, _Window] = {}
        self._anchors: Dict[str, float] = {}
        self._penalized_at: Dict[str, float] = {}
        self._loaded: set[str] = set()
        self._last_flush = wall()
        self._last_log = wall()

    # --- window -----------------------------------------------------------------------------
    def _window_start(self, key: str, now: float) -> float:
        anchor = self._anchors.get(key, 0.0)
        return anchor + math.floor((now - anchor) / DAY_SECS) * DAY_SECS

    def _window(self, key: str) -> _Window:
        self._load(key)
        now = self._wall()
        start = self._window_start(key, now)
        window = self._windows.get(key)
        if window is None or window.start != start:
            if window is not None:
                self._flush_key(key, window)
                logger.info("Telegram chat %s daily window closed: %d messages, %d calls %s", key,
                            window.messages, sum(window.calls.values()), _top(window.calls))
            window = self._windows[key] = _Window(start)
        return window

    # --- counting ---------------------------------------------------------------------------
    def record(self, chat_key: str, endpoint: str, data: Any = None) -> None:
        window = self._window(chat_key)
        window.calls[endpoint] = window.calls.get(endpoint, 0) + 1
        window.dirty[endpoint] = window.dirty.get(endpoint, 0) + 1
        created = messages_created(endpoint, data)
        window.messages += created
        window.dirty_messages += created
        now = self._wall()
        if now - self._last_flush >= self._flush_interval:
            self.flush()
        if now - self._last_log >= LOG_INTERVAL_SECS:
            self._last_log = now
            self.log_summary()

    def messages(self, chat_key: str) -> int:
        return self._window(chat_key).messages

    def calls(self, chat_key: str) -> Dict[str, int]:
        return dict(self._window(chat_key).calls)

    def pressure(self, chat_key: str) -> int:
        if not self.soft_ceiling or self.soft_ceiling <= 0:
            return PRESSURE_NONE
        used = self.messages(chat_key)
        if used >= self.soft_ceiling:
            return PRESSURE_NOTICES
        if used >= self.soft_ceiling * COSMETIC_FRACTION:
            return PRESSURE_COSMETIC
        return PRESSURE_NONE

    def call_ceiling(self, chat_key: str) -> Optional[float]:
        """Today's call budget, scaled down while a recent long penalty is in force. None = off."""
        if not self.call_soft_ceiling or self.call_soft_ceiling <= 0:
            return None
        self._load(chat_key)
        penalized_at = self._penalized_at.get(chat_key)
        if penalized_at is not None and self._wall() - penalized_at < STRICT_WINDOW_SECS:
            return self.call_soft_ceiling * STRICT_FACTOR
        return float(self.call_soft_ceiling)

    def call_pressure(self, chat_key: str) -> float:
        """Fraction of today's call budget spent (0 when the budget is off)."""
        ceiling = self.call_ceiling(chat_key)
        if ceiling is None:
            return 0.0
        return sum(self._window(chat_key).calls.values()) / ceiling

    def sheds(self, chat_key: str, kind: Optional[str], trigger: Optional[str] = None) -> bool:
        """Whether a call of this outbound class must be shed now. Finals (``None``) never are.

        Typed turns keep the initial tool bubble and notices. Their typing, draft and interim text
        traffic is shed when the day's call budget is spent. Background producers shed cosmetics at
        half the call budget or at the message rule, and notices at the full budget.
        """
        if kind is None or kind == OUTBOUND_FINAL:
            return False
        # Untagged traffic (busy acks, button taps, restart and recovery notes, cron delivery) is a
        # few hundred calls a day and mostly answers the user, so only the message rule meters it.
        calls = 0.0 if trigger == TRIGGER_UNTAGGED else self.call_pressure(chat_key)
        if trigger == "typed":
            return kind not in (OUTBOUND_NOTICE, OUTBOUND_TOOL_PROGRESS) and calls >= 1.0
        pressure = self.pressure(chat_key)
        if kind == OUTBOUND_NOTICE:
            return pressure >= PRESSURE_NOTICES or calls >= 1.0
        # progress, typing, interim edits, drafts
        return pressure >= PRESSURE_COSMETIC or calls >= BACKGROUND_CALL_FRACTION

    def note_retry_after(self, chat_key: str, wait: float) -> None:
        """A long server penalty ends at the window reset: anchor there and record the evidence."""
        window = self._window(chat_key)
        logger.warning(
            "Telegram chat %s refused for %.0fs after %d messages and %d calls in the window since %s %s",
            chat_key, wait, window.messages, sum(window.calls.values()),
            time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(window.start)), _top(window.calls))
        if wait >= STRICT_PENALTY_SECS:
            # The strict day starts when sending resumes; time inside the ban spends nothing.
            # A shorter penalty landing inside a longer one must not pull that end earlier.
            self._penalized_at[chat_key] = max(self._wall() + wait, self._penalized_at.get(chat_key, 0.0))
            self._persist_penalty(chat_key)
        if wait < RESET_PENALTY_SECS:
            return
        reset = self._wall() + wait
        self._anchors[chat_key] = reset % DAY_SECS
        self._persist_anchor(chat_key)
        # The traffic so far belongs to the window that ends at this reset: carry it over whole.
        self.flush()
        window.start = self._window_start(chat_key, self._wall())
        window.dirty = dict(window.calls)
        window.dirty_messages = window.messages
        self._flush_key(chat_key, window)

    def log_summary(self) -> None:
        for key, window in list(self._windows.items()):
            ceiling = self.call_ceiling(key)
            logger.info("Telegram chat %s daily volume since %s: %d messages (soft ceiling %s), %d calls "
                        "(call ceiling %s) %s",
                        key, time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(window.start)), window.messages,
                        self.soft_ceiling, sum(window.calls.values()),
                        "off" if ceiling is None else f"{ceiling:.0f}", _top(window.calls))

    # --- persistence ------------------------------------------------------------------------
    def _db(self):
        path = self._profile_dir / "telegram-flood-state.db"
        conn = sqlite3.connect(path, timeout=1.0)
        conn.execute("CREATE TABLE IF NOT EXISTS daily_volume (chat_id TEXT NOT NULL, window_start REAL NOT NULL, "
                     "endpoint TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (chat_id, window_start, endpoint))")
        conn.execute("CREATE TABLE IF NOT EXISTS daily_anchor (chat_id TEXT PRIMARY KEY, anchor REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS daily_penalty (chat_id TEXT PRIMARY KEY, at REAL NOT NULL)")
        return closing(conn)

    def _load(self, key: str) -> None:
        if key in self._loaded:
            return
        if self._profile_dir is None:
            self._loaded.add(key)
            return
        try:
            with self._db() as conn:
                row = conn.execute("SELECT anchor FROM daily_anchor WHERE chat_id=?", (key,)).fetchone()
                if row is not None:
                    self._anchors[key] = float(row[0])
                row = conn.execute("SELECT at FROM daily_penalty WHERE chat_id=?", (key,)).fetchone()
                if row is not None:
                    self._penalized_at[key] = max(float(row[0]), self._penalized_at.get(key, 0.0))
                start = self._window_start(key, self._wall())
                rows = conn.execute("SELECT endpoint, count FROM daily_volume WHERE chat_id=? AND window_start=?",
                                    (key, start)).fetchall()
        except (OSError, sqlite3.Error):
            # Not marked loaded: the next call retries, so a transient lock cannot hide the day's counts.
            logger.warning("Could not load Telegram daily volume for chat %s", key, exc_info=True)
            return
        self._loaded.add(key)
        window = _Window(start)
        for endpoint, count in rows:
            if endpoint == "__messages__":
                window.messages = int(count)
            else:
                window.calls[endpoint] = int(count)
        # Calls counted in memory while the store was unreadable are not in the rows yet: keep them.
        pending = self._windows.get(key)
        if pending is not None and pending.start == start:
            window.dirty, window.dirty_messages = pending.dirty, pending.dirty_messages
            window.messages += pending.dirty_messages
            for endpoint, count in pending.dirty.items():
                window.calls[endpoint] = window.calls.get(endpoint, 0) + count
        self._windows[key] = window

    def _flush_key(self, key: str, window: _Window) -> None:
        if self._profile_dir is None:
            window.dirty.clear()
            window.dirty_messages = 0
            return
        deltas = dict(window.dirty)
        if window.dirty_messages:
            deltas["__messages__"] = window.dirty_messages
        try:
            with self._db() as conn:
                if deltas:
                    conn.executemany(
                        "INSERT INTO daily_volume VALUES (?,?,?,?) ON CONFLICT(chat_id, window_start, endpoint) "
                        "DO UPDATE SET count = count + excluded.count",
                        [(key, window.start, endpoint, count) for endpoint, count in deltas.items()])
                    conn.execute("DELETE FROM daily_volume WHERE window_start < ?", (self._wall() - 14 * DAY_SECS,))
                    conn.commit()
                # Read the shared totals back: another lane (the standalone sender) spends the same day.
                totals = conn.execute("SELECT endpoint, count FROM daily_volume WHERE chat_id=? AND window_start=?",
                                      (key, window.start)).fetchall()
                penalty = conn.execute("SELECT at FROM daily_penalty WHERE chat_id=?", (key,)).fetchone()
        except (OSError, sqlite3.Error):
            logger.warning("Could not persist Telegram daily volume for chat %s", key, exc_info=True)
            return
        window.dirty.clear()
        window.dirty_messages = 0
        if penalty is not None:  # the standalone lane records penalties too
            self._penalized_at[key] = max(float(penalty[0]), self._penalized_at.get(key, 0.0))
        for endpoint, count in totals:
            if endpoint == "__messages__":
                window.messages = int(count)
            else:
                window.calls[endpoint] = int(count)

    def flush(self) -> None:
        self._last_flush = self._wall()
        for key, window in list(self._windows.items()):
            self._flush_key(key, window)

    def _persist_anchor(self, key: str) -> None:
        if self._profile_dir is None:
            return
        try:
            with self._db() as conn:
                conn.execute("INSERT INTO daily_anchor VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET anchor=excluded.anchor",
                             (key, self._anchors[key]))
                conn.commit()
        except (OSError, sqlite3.Error):
            logger.warning("Could not persist Telegram daily window anchor for chat %s", key, exc_info=True)

    def _persist_penalty(self, key: str) -> None:
        if self._profile_dir is None:
            return
        try:
            with self._db() as conn:
                conn.execute("INSERT INTO daily_penalty VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET at=MAX(at, excluded.at)",
                             (key, self._penalized_at[key]))
                conn.commit()
        except (OSError, sqlite3.Error):
            logger.warning("Could not persist Telegram long penalty for chat %s", key, exc_info=True)


def send_outbound_class(explicit: Optional[str], metadata: Any, trigger: Optional[str]) -> Optional[str]:
    """Outbound class for a plain send: the explicit label, else progress for an ``_interim_send``.
    A typed turn's unlabelled interim sends include /login codes, approval fallbacks and stuck
    warnings, so they stay unclassed (never shed); only labelled progress sheds for typed turns."""
    if explicit:
        return explicit
    if isinstance(metadata, dict) and metadata.get("_interim_send") and trigger != "typed":
        return OUTBOUND_PROGRESS
    return None


def _extra_int(extra: Any, key: str, default: int, label: str) -> int:
    raw = extra.get(key, default) if isinstance(extra, dict) else None
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        logger.warning("[%s] Invalid %s %r; using %d", label, key, raw, default)
        return default


def quota_from_platform_extra(extra: Any, *, profile_dir: Optional[Path], label: str) -> DailyQuota:
    """The adapter's ledger: ``daily_message_soft_ceiling`` and ``daily_call_soft_ceiling`` in the
    platform extra set the budgets (0 disables that budget's shedding, counting continues)."""
    return DailyQuota(
        soft_ceiling=_extra_int(extra, "daily_message_soft_ceiling", DEFAULT_SOFT_CEILING, label),
        call_soft_ceiling=_extra_int(extra, "daily_call_soft_ceiling", DEFAULT_CALL_SOFT_CEILING, label),
        profile_dir=profile_dir)

def _top(calls: Dict[str, int], n: int = 6) -> str:
    items = sorted(calls.items(), key=lambda kv: -kv[1])[:n]
    return json.dumps(dict(items), separators=(",", ":"))
