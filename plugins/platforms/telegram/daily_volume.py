"""Per-chat daily Bot API volume ledger: measurement only, with no shedding.

The per-minute budget in ``chat_budget.py`` did not prevent multi-hour bans. On 2026-10-04 and
2026-10-05 the DM was refused for 24237s and 34507s while traffic was 0-6 sends/min. Every long
ban since 2026-09-28 has ended at a fixed time of day: 09:33:23-24 UTC on two consecutive days,
then 09:58:45 and 09:58:59. That suggests a daily volume cap, which Telegram does not document.
This ledger counts every metered call per chat and per window, by endpoint, so the next refusal
can be compared against the day's actual volume.

- The window is a rolling 24h. Its anchor defaults to UTC midnight. A published ``retry_after`` of
  an hour or more moves the anchor to the moment Telegram named, which is the reset.
- Counts persist in the profile's ``telegram-flood-state.db``, so a restart does not zero the day
  and the standalone sender's calls are included.
- Totals are logged at INFO hourly, and at WARNING with every refusal.
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
LOG_INTERVAL_SECS = 3600.0
FLUSH_INTERVAL_SECS = 60.0
# A server penalty this long ends at a window reset rather than after a burst, so it re-anchors.
RESET_PENALTY_SECS = 3600.0
RETENTION_SECS = 14 * DAY_SECS
_MESSAGES_ROW = "__messages__"

# Endpoints that create a message in the chat. A media group creates one message per item.
_MESSAGE_ENDPOINTS = frozenset({
    "sendMessage", "sendRichMessage", "sendPhoto", "sendDocument", "sendVideo", "sendVoice", "sendAudio",
    "sendAnimation", "sendSticker", "sendVideoNote", "sendLocation", "sendVenue", "sendContact", "sendPoll",
    "sendDice", "sendMediaGroup", "copyMessage", "forwardMessage", "copyMessages", "forwardMessages",
})


def messages_created(endpoint: str, data: Any) -> int:
    if endpoint not in _MESSAGE_ENDPOINTS:
        return 0
    if isinstance(data, dict):
        items = data.get("media") if endpoint == "sendMediaGroup" else (
            data.get("message_ids") if endpoint in ("copyMessages", "forwardMessages") else None)
        if isinstance(items, (list, tuple)) and items:
            return len(items)
    return 1


@dataclass
class _Window:
    start: float
    calls: Dict[str, int] = field(default_factory=dict)
    messages: int = 0
    dirty: Dict[str, int] = field(default_factory=dict)
    dirty_messages: int = 0


class DailyVolume:
    """Count calls per chat over a rolling 24h window, persisted and shared across processes."""

    def __init__(self, *, profile_dir: Optional[Path] = None, wall: Callable[[], float] = time.time,
                 flush_interval: float = FLUSH_INTERVAL_SECS):
        self._profile_dir = Path(profile_dir) if profile_dir is not None else None
        self._wall = wall
        self._flush_interval = flush_interval
        self._windows: Dict[str, _Window] = {}
        self._anchors: Dict[str, float] = {}
        self._loaded: set[str] = set()
        self._last_flush = self._last_log = wall()

    def _window_start(self, key: str, now: float) -> float:
        anchor = self._anchors.get(key, 0.0)
        return anchor + math.floor((now - anchor) / DAY_SECS) * DAY_SECS

    def _window(self, key: str) -> _Window:
        self._load(key)
        start = self._window_start(key, self._wall())
        window = self._windows.get(key)
        if window is None or window.start != start:
            if window is not None:
                self._flush_key(key, window)
                logger.info("Telegram chat %s daily window closed: %d messages, %d calls %s", key,
                            window.messages, sum(window.calls.values()), _top(window.calls))
            window = self._windows[key] = _Window(start)
        return window

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

    def note_retry_after(self, chat_key: str, wait: float) -> None:
        """Log the window's volume at the refusal. A long penalty anchors the window at its end."""
        window = self._window(chat_key)
        logger.warning(
            "Telegram chat %s refused for %.0fs after %d messages and %d calls in the window since %s %s",
            chat_key, wait, window.messages, sum(window.calls.values()), _stamp(window.start), _top(window.calls))
        if wait < RESET_PENALTY_SECS:
            return
        self._anchors[chat_key] = (self._wall() + wait) % DAY_SECS
        self._persist_anchor(chat_key)
        # The traffic so far belongs to the window that ends at this reset: carry it over whole.
        self.flush()
        window.start = self._window_start(chat_key, self._wall())
        window.dirty, window.dirty_messages = dict(window.calls), window.messages
        self._flush_key(chat_key, window)

    def log_summary(self) -> None:
        for key, window in list(self._windows.items()):
            logger.info("Telegram chat %s daily volume since %s: %d messages, %d calls %s", key,
                        _stamp(window.start), window.messages, sum(window.calls.values()), _top(window.calls))

    def flush(self) -> None:
        self._last_flush = self._wall()
        for key, window in list(self._windows.items()):
            self._flush_key(key, window)

    # --- persistence ------------------------------------------------------------------------
    def _db(self):
        conn = sqlite3.connect(self._profile_dir / "telegram-flood-state.db", timeout=1.0)
        conn.execute("CREATE TABLE IF NOT EXISTS daily_volume (chat_id TEXT NOT NULL, window_start REAL NOT NULL, "
                     "endpoint TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (chat_id, window_start, endpoint))")
        conn.execute("CREATE TABLE IF NOT EXISTS daily_anchor (chat_id TEXT PRIMARY KEY, anchor REAL NOT NULL)")
        return closing(conn)

    def _apply(self, window: _Window, rows) -> None:
        for endpoint, count in rows:
            if endpoint == _MESSAGES_ROW:
                window.messages = int(count)
            else:
                window.calls[endpoint] = int(count)

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
        self._apply(window, rows)

    def _flush_key(self, key: str, window: _Window) -> None:
        if self._profile_dir is None:
            window.dirty.clear()
            window.dirty_messages = 0
            return
        deltas = dict(window.dirty)
        if window.dirty_messages:
            deltas[_MESSAGES_ROW] = window.dirty_messages
        try:
            with self._db() as conn:
                if deltas:
                    conn.executemany(
                        "INSERT INTO daily_volume VALUES (?,?,?,?) ON CONFLICT(chat_id, window_start, endpoint) "
                        "DO UPDATE SET count = count + excluded.count",
                        [(key, window.start, endpoint, count) for endpoint, count in deltas.items()])
                    conn.execute("DELETE FROM daily_volume WHERE window_start < ?", (self._wall() - RETENTION_SECS,))
                    conn.commit()
                # Read the shared totals back: the standalone sender spends the same day.
                totals = conn.execute("SELECT endpoint, count FROM daily_volume WHERE chat_id=? AND window_start=?",
                                      (key, window.start)).fetchall()
        except (OSError, sqlite3.Error):
            logger.warning("Could not persist Telegram daily volume for chat %s", key, exc_info=True)
            return
        window.dirty.clear()
        window.dirty_messages = 0
        self._apply(window, totals)

    def _persist_anchor(self, key: str) -> None:
        if self._profile_dir is None:
            return
        try:
            with self._db() as conn:
                conn.execute("INSERT INTO daily_anchor VALUES (?, ?) "
                             "ON CONFLICT(chat_id) DO UPDATE SET anchor=excluded.anchor", (key, self._anchors[key]))
                conn.commit()
        except (OSError, sqlite3.Error):
            logger.warning("Could not persist Telegram daily window anchor for chat %s", key, exc_info=True)


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))


def _top(calls: Dict[str, int], n: int = 8) -> str:
    return json.dumps(dict(sorted(calls.items(), key=lambda kv: -kv[1])[:n]), separators=(",", ":"))
