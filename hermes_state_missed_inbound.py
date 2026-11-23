"""Durable ``state_meta`` ledger for inbound messages missed while a platform was disconnected.

Two record kinds, both owned by ``gateway/missed_inbound_recovery.py``:

- a per-platform *watermark*: the last moment the platform was known connected. The next
  recovery scan starts there, so it covers the offline interval and never the uptime before it.
- a per-message *claim* keyed by platform, conversation and message timestamp. A claim is
  written before a missed message is handed to the inbound handler (``late``), and when a
  message arrives live (``live``). Claims are never released, so a message is recovered at
  most once across restarts and crash loops, and never after it was already handled live.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Protocol


class _SessionDBAPI(Protocol):
    def _read_ctx(self) -> Any: ...
    def _execute_write(self, fn: Any) -> Any: ...


_WATERMARK_PREFIX = "missed_inbound:watermark:"
_CLAIM_PREFIX = "missed_inbound:claim:"
_LATE, _LIVE = "late", "live"  # same length: the claim time starts at a fixed offset


class SessionMissedInboundMixin:
    """Watermarks and at-most-once claims for missed inbound recovery."""

    def get_missed_inbound_watermark(self: _SessionDBAPI, platform: str) -> Optional[float]:
        """Last moment *platform* was known connected, or None before its first connection."""
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key=?", (_WATERMARK_PREFIX + platform,)).fetchone()
        try:
            return float(row[0]) if row is not None else None
        except (TypeError, ValueError):
            return None

    def advance_missed_inbound_watermark(self: _SessionDBAPI, platform: str, at: float) -> None:
        """Move the watermark forward to *at*. It never moves backwards."""
        self._execute_write(lambda conn: conn.execute(
            "INSERT INTO state_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
            "WHERE CAST(state_meta.value AS REAL) < CAST(excluded.value AS REAL)",
            (_WATERMARK_PREFIX + platform, repr(float(at)))))

    def claim_missed_inbound(
            self: _SessionDBAPI, platform: str, conversation: str, message_ts: str, *, live: bool = False) -> bool:
        """Claim one message. True only for the first claim of that platform, conversation and ts."""
        key = f"{_CLAIM_PREFIX}{platform}:{conversation}:{message_ts}"
        value = f"{_LIVE if live else _LATE}:{time.time()!r}"
        return bool(self._execute_write(lambda conn: conn.execute(
            "INSERT OR IGNORE INTO state_meta(key, value) VALUES (?, ?)", (key, value)).rowcount == 1))

    def list_recovered_missed_inbound(self: _SessionDBAPI, platform: str) -> list[tuple[str, str]]:
        """``(conversation, ts)`` of every message recovery has claimed for *platform*."""
        prefix = f"{_CLAIM_PREFIX}{platform}:"
        with self._read_ctx() as conn:
            rows = conn.execute(
                "SELECT key FROM state_meta WHERE key GLOB ? AND value GLOB ?",
                (prefix + "*", _LATE + ":*")).fetchall()
        pairs = (str(row[0])[len(prefix):].rpartition(":") for row in rows)
        return [(conversation, ts) for conversation, _, ts in pairs if conversation and ts]

    def prune_missed_inbound_claims(self: _SessionDBAPI, older_than: float) -> int:
        """Drop claims written before *older_than*. Scans never look back further than 24 hours."""
        return int(self._execute_write(lambda conn: conn.execute(
            "DELETE FROM state_meta WHERE key GLOB ? AND CAST(substr(value, ?) AS REAL) < ?",
            (_CLAIM_PREFIX + "*", len(_LATE) + 2, float(older_than))).rowcount) or 0)
