"""Topical helpers extracted from hermes_cli/goals.py."""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Iterable, Optional

def _relay_sender(content: str) -> Optional[tuple[str, str]]:
    """Return ``(profile, session_id)`` from a Hermes relay header on the first or last non-empty line."""
    from hermes_cli.goals import _unwrap_steer, _RELAY_HEADER_RE
    if not isinstance(content, str):
        return None
    lines = [line.strip() for line in _unwrap_steer(content).splitlines() if line.strip()]
    for line in (lines[-1:] + lines[:1]) if lines else []:
        match = _RELAY_HEADER_RE.fullmatch(line)
        if match:
            return match.group("profile"), match.group("session")
    return None


def _relay_receipt_id(content: str) -> str:
    from hermes_cli.goals import re, _unwrap_steer, _RELAY_HEADER_RE
    lines = [line.strip() for line in _unwrap_steer(str(content or "")).splitlines() if line.strip()]
    for line in (lines[-1:] + lines[:1]) if lines else []:
        match = re.search(r"\breceipt=([^\s\]]+)", line) if _RELAY_HEADER_RE.fullmatch(line) else None
        if match:
            return match.group(1)
    return ""


# The relay ledger is append-only in normal operation, and a goal evaluation used to re-read all of
# it (tens of MB) per call. Keep a process-local ``receipt id -> (offset, length)`` index and consume
# only the bytes appended since the last call; a lookup then reads just the wanted rows. ``read_offset``
# is the end of the consumed bytes and an unterminated final line waits in ``pending`` until its newline
# lands. A smaller file, a different inode, or an unchanged size with a new mtime rebuilds the index.
_RELAY_RECEIPT_CACHE_LOCK = threading.RLock()
_RELAY_RECEIPT_CACHE: dict[str, dict[str, Any]] = {}
_RELAY_ROW_PREFIX = b'{"id":"'


def _relay_line_id(line: bytes) -> Optional[str]:
    """Receipt id of one ledger line, or None. Relay writes ``id`` first, so the common row is
    sliced without a JSON parse; anything unusual falls back to a full parse."""
    if line.startswith(_RELAY_ROW_PREFIX) and line.endswith(b"}"):
        end = line.find(b'"', len(_RELAY_ROW_PREFIX))
        raw = line[len(_RELAY_ROW_PREFIX):end]
        if end > 0 and b"\\" not in raw:
            return raw.decode("utf-8", "replace")
    try:
        row = json.loads(line.decode("utf-8-sig", "replace"))
    except ValueError:
        return None
    rid = row.get("id") if isinstance(row, dict) else None
    return rid if isinstance(rid, str) and rid else None


def _relay_receipt_index(path: str) -> tuple[dict[str, tuple[int, int]], bool]:
    """``(id -> (offset, length), ok)`` for the ledger at *path*, advanced to its current size."""
    try:
        stat = os.stat(path)
    except OSError:
        return {}, False
    identity = (stat.st_dev, stat.st_ino)
    with _RELAY_RECEIPT_CACHE_LOCK:
        cached = _RELAY_RECEIPT_CACHE.get(path)
        if (cached is None or cached["identity"] != identity
                or stat.st_size < cached["read_offset"]
                or (stat.st_size == cached["read_offset"] and stat.st_mtime_ns != cached["mtime_ns"])):
            cached = {"identity": identity, "read_offset": 0, "mtime_ns": 0, "pending": b"", "rows": {}}
            _RELAY_RECEIPT_CACHE[path] = cached
        try:
            with open(path, "rb") as handle:
                handle.seek(cached["read_offset"])
                added = handle.read()
        except OSError:
            return {}, False
        if added:
            data = cached["pending"] + added
            offset = cached["read_offset"] - len(cached["pending"])
            complete, _, cached["pending"] = data.rpartition(b"\n")
            if _:
                for line in complete.split(b"\n"):
                    rid = _relay_line_id(line)
                    if rid is not None:
                        cached["rows"][rid] = (offset, len(line))
                    offset += len(line) + 1
            else:
                cached["pending"] = data
            cached["read_offset"] += len(added)
        cached["mtime_ns"] = stat.st_mtime_ns
        return cached["rows"], True


def _relay_ledger_receipts(receipt_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Newest receipt row per wanted id from relay's append-only ledger.

    The header in a transcript is plain text any agent can write; the ledger is written by the relay
    transport at send time and records sender, target and the delivered transcript row. The cache
    is invalidated on rotation or truncation and otherwise parses only appended lines.
    """
    wanted = {rid for rid in receipt_ids if rid}
    if not wanted:
        return {}
    state_dir = os.environ.get("RELAY_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".local/state/relay")
    path = os.path.join(state_dir, "receipts.jsonl")
    with _RELAY_RECEIPT_CACHE_LOCK:
        rows, ok = _relay_receipt_index(path)
        spans = {rid: rows[rid] for rid in wanted if rid in rows}
    found: dict[str, dict[str, Any]] = {}
    if not ok or not spans:
        return found
    try:
        with open(path, "rb") as handle:
            for rid, (offset, length) in spans.items():
                handle.seek(offset)
                try:
                    row = json.loads(handle.read(length).decode("utf-8-sig", "replace"))
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("id") == rid:
                    found[rid] = row
    except OSError:
        return {}
    return found
