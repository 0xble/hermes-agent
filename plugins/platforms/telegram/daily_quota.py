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
- At ``COSMETIC_FRACTION`` of the ceiling, typing, interim edits, drafts, progress bubbles and
  their cleanup deletes are shed.
- At the ceiling, non-final notices are also shed.
- Final replies are never shed.
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
LOG_INTERVAL_SECS = 3600.0
FLUSH_INTERVAL_SECS = 60.0
# A server penalty this long is a window reset, not a burst penalty, so it re-anchors the window.
RESET_PENALTY_SECS = 3600.0

PRESSURE_NONE = 0
PRESSURE_COSMETIC = 1
PRESSURE_NOTICES = 2

from gateway.platforms.base import (  # noqa: E402
    OUTBOUND_FINAL, OUTBOUND_NOTICE, OUTBOUND_PROGRESS, current_outbound_class, outbound_class)

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
                 wall: Callable[[], float] = time.time, flush_interval: float = FLUSH_INTERVAL_SECS):
        self.soft_ceiling = soft_ceiling
        self._profile_dir = Path(profile_dir) if profile_dir is not None else None
        self._wall = wall
        self._flush_interval = flush_interval
        self._windows: Dict[str, _Window] = {}
        self._anchors: Dict[str, float] = {}
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

    def sheds(self, chat_key: str, kind: Optional[str], trigger: Optional[str] = None) -> bool:
        """Whether a call of this outbound class must be shed now. Finals (``None``) never are.

        Normal typed turns keep their progress and notice UX. Pressure shedding applies to autonomous
        background producers, which caused the recurrence, while final replies remain unshed everywhere.
        """
        if trigger == "typed":
            return False
        if kind is None or kind == OUTBOUND_FINAL:
            return False
        pressure = self.pressure(chat_key)
        if kind == OUTBOUND_NOTICE:
            return pressure >= PRESSURE_NOTICES
        return pressure >= PRESSURE_COSMETIC  # progress, typing, interim edits, drafts, cleanup

    def note_retry_after(self, chat_key: str, wait: float) -> None:
        """A long server penalty ends at the window reset: anchor there and record the evidence."""
        window = self._window(chat_key)
        logger.warning(
            "Telegram chat %s refused for %.0fs after %d messages and %d calls in the window since %s %s",
            chat_key, wait, window.messages, sum(window.calls.values()),
            time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(window.start)), _top(window.calls))
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
            logger.info("Telegram chat %s daily volume since %s: %d messages (soft ceiling %s), %d calls %s",
                        key, time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(window.start)), window.messages,
                        self.soft_ceiling, sum(window.calls.values()), _top(window.calls))

    # --- persistence ------------------------------------------------------------------------
    def _db(self):
        path = self._profile_dir / "telegram-flood-state.db"
        conn = sqlite3.connect(path, timeout=1.0)
        conn.execute("CREATE TABLE IF NOT EXISTS daily_volume (chat_id TEXT NOT NULL, window_start REAL NOT NULL, "
                     "endpoint TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (chat_id, window_start, endpoint))")
        conn.execute("CREATE TABLE IF NOT EXISTS daily_anchor (chat_id TEXT PRIMARY KEY, anchor REAL NOT NULL)")
        return closing(conn)

    def _load(self, key: str) -> None:
        if key in self._loaded:
            return
        self._loaded.add(key)
        if self._profile_dir is None:
            return
        try:
            with self._db() as conn:
                row = conn.execute("SELECT anchor FROM daily_anchor WHERE chat_id=?", (key,)).fetchone()
                if row is not None:
                    self._anchors[key] = float(row[0])
                start = self._window_start(key, self._wall())
                rows = conn.execute("SELECT endpoint, count FROM daily_volume WHERE chat_id=? AND window_start=?",
                                    (key, start)).fetchall()
        except (OSError, sqlite3.Error):
            logger.warning("Could not load Telegram daily volume for chat %s", key, exc_info=True)
            return
        window = self._windows[key] = _Window(start)
        for endpoint, count in rows:
            if endpoint == "__messages__":
                window.messages = int(count)
            else:
                window.calls[endpoint] = int(count)

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
        except (OSError, sqlite3.Error):
            logger.warning("Could not persist Telegram daily volume for chat %s", key, exc_info=True)
            return
        window.dirty.clear()
        window.dirty_messages = 0
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


def _top(calls: Dict[str, int], n: int = 6) -> str:
    items = sorted(calls.items(), key=lambda kv: -kv[1])[:n]
    return json.dumps(dict(items), separators=(",", ":"))
