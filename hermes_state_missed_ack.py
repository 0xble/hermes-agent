"""Durable state for acknowledgements of messages missed while the gateway was offline."""

from __future__ import annotations

import json
import time
from typing import Any, Optional, Protocol


class _SessionDBAPI(Protocol):
    @classmethod
    def _missed_ack_key(cls, kind: str, *parts: str) -> str: ...
    def _read_ctx(self) -> Any: ...
    def _execute_write(self, operation: Any) -> Any: ...


class SessionMissedMessageAckMixin:
    """Small state_meta-backed ledger for platform recovery hooks."""

    _MISSED_ACK_PREFIX = "missed_message_ack:"

    @classmethod
    def _missed_ack_key(cls, kind: str, *parts: str) -> str:
        return cls._MISSED_ACK_PREFIX + ":".join([kind, *[str(part) for part in parts]])

    def get_missed_message_ack_state(self: _SessionDBAPI, kind: str, *parts: str) -> Optional[Any]:
        key = self._missed_ack_key(kind, *parts)
        with self._read_ctx() as conn:
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return None

    def set_missed_message_ack_state(self: _SessionDBAPI, kind: str, value: Any, *parts: str) -> None:
        key = self._missed_ack_key(kind, *parts)
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        self._execute_write(lambda conn: conn.execute(
            "INSERT INTO state_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encoded),
        ))

    def claim_missed_message_ack(
        self: _SessionDBAPI, platform: str, channel_id: str, message_ts: str, *, stale_after: float = 3600.0,
    ) -> bool:
        """Reserve one outbound ack, atomically, before its network call.

        A stale reservation is reclaimable. If a process dies after Slack accepted the post,
        the next scan checks Slack's thread and sees the bot reply before trying again.
        """
        key = self._missed_ack_key("message", platform, channel_id, message_ts)
        now = time.time()

        def write(conn) -> bool:
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            if row is not None:
                try:
                    saved = json.loads(row[0])
                except (TypeError, ValueError):
                    saved = {}
                if saved.get("status") == "sent":
                    return False
                if saved.get("status") == "reserved" and now - float(saved.get("at", now)) < stale_after:
                    return False
            conn.execute(
                "INSERT INTO state_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps({"at": now, "status": "reserved"}, separators=(",", ":"))),
            )
            return True

        return bool(self._execute_write(write))

    def mark_missed_message_ack_sent(self: _SessionDBAPI, platform: str, channel_id: str, message_ts: str) -> None:
        key = self._missed_ack_key("message", platform, channel_id, message_ts)
        self._execute_write(lambda conn: conn.execute(
            "UPDATE state_meta SET value=? WHERE key=?",
            (json.dumps({"at": time.time(), "status": "sent"}, separators=(",", ":")), key),
        ))

    def release_missed_message_ack(self: _SessionDBAPI, platform: str, channel_id: str, message_ts: str) -> None:
        key = self._missed_ack_key("message", platform, channel_id, message_ts)
        self._execute_write(lambda conn: conn.execute(
            "DELETE FROM state_meta WHERE key=? AND json_extract(value, '$.status')='reserved'", (key,)
        ))
