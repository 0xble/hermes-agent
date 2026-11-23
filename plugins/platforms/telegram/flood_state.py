"""Profile-local Telegram flood deadlines shared across adapter lifetimes.

Telegram's retry_after is a server deadline, not an inline sleep budget. SQLite
keeps the longest observed deadline for each chat across gateway restarts and
serializes concurrent writers without a process-global lock.
"""

from contextlib import closing
import hashlib
import math
from pathlib import Path
import os
import sqlite3
import time
import json


def storage_key(chat_id: str, token: str | None) -> str:
    """Key durable state by the public bot ID, never by the credential itself."""
    bot_id, sep, _ = (token or "").partition(":")
    return f"{bot_id}:{chat_id}" if sep and bot_id.isascii() and bot_id.isdigit() else chat_id


def _fallback_dir(profile_dir: Path, chat_id: str) -> Path:
    name = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
    return Path(profile_dir) / "telegram-flood-deadlines" / name


def fallback_remaining_seconds(profile_dir: Path, chat_id: str) -> float:
    """Read the emergency deadline used when the SQLite writer was unavailable."""
    directory = _fallback_dir(profile_dir, chat_id)
    if not directory.exists():
        return 0.0
    latest = 0.0
    for path in directory.glob("*.deadline"):
        until = float(path.read_text(encoding="ascii"))
        if not math.isfinite(until):
            raise ValueError("invalid Telegram flood fallback deadline")
        latest = max(latest, until)
    return max(0.0, latest - time.time())


def record_fallback_deadline(profile_dir: Path, chat_id: str, wait: float) -> float:
    """Atomically save a deadline if the primary SQLite store cannot be written."""
    directory = _fallback_dir(profile_dir, chat_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    until = time.time() + wait
    name = f"{os.getpid()}.{time.time_ns()}"
    temporary = directory / f"{name}.tmp"
    path = directory / f"{name}.deadline"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(repr(until))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return until


def record_deadline(profile_dir: Path, chat_id: str, wait: float) -> float:
    """Persist the later of the existing deadline and this refusal; return it."""
    path = Path(profile_dir) / "telegram-flood-state.db"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    deadline = time.time() + wait
    with closing(sqlite3.connect(path, timeout=1.0)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS deadlines (chat_id TEXT PRIMARY KEY, until REAL NOT NULL)")
        conn.execute("DELETE FROM deadlines WHERE until <= ?", (time.time(),))
        conn.execute(
            "INSERT INTO deadlines(chat_id, until) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET until = MAX(until, excluded.until)",
            (chat_id, deadline),
        )
        stored = conn.execute("SELECT until FROM deadlines WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        conn.commit()
    return stored


def remaining_seconds(profile_dir: Path, chat_id: str) -> float:
    """Return the persisted remaining wait, or zero when no deadline exists.

    Read the pre-SQLite per-bot JSON receipt as a compatibility fallback. Existing gateways may
    still have that file on disk after upgrading; its wall-clock entries are clamped to the same
    five-minute safety window as newly restored state.
    """
    path = Path(profile_dir) / "telegram-flood-state.db"
    if path.exists():
        with closing(sqlite3.connect(path, timeout=1.0)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS deadlines (chat_id TEXT PRIMARY KEY, until REAL NOT NULL)")
            row = conn.execute("SELECT until FROM deadlines WHERE chat_id = ?", (chat_id,)).fetchone()
            if row is not None:
                remaining = row[0] - time.time()
                if remaining <= 0:
                    conn.execute("DELETE FROM deadlines WHERE chat_id = ? AND until = ?", (chat_id, row[0]))
                    conn.commit()
                    newer = conn.execute("SELECT until FROM deadlines WHERE chat_id = ?", (chat_id,)).fetchone()
                    return max(0.0, newer[0] - time.time()) if newer else 0.0
                return remaining

    bot_id, sep, raw_chat = str(chat_id).partition(":")
    if not sep or not bot_id.isdigit():
        raw_chat = str(chat_id)
        bot_id = ""
    legacy = Path(profile_dir) / f"telegram_flood_cooldown_{bot_id}.json" if bot_id else None
    if legacy is None or not legacy.exists():
        return 0.0
    try:
        saved = json.loads(legacy.read_text(encoding="utf-8-sig")).get("until") or {}
        deadline = float(saved.get(raw_chat, 0.0)) if isinstance(saved, dict) else 0.0
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0
    return max(0.0, min(deadline - time.time(), 300.0))
