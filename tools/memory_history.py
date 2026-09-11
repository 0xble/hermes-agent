"""Durable, conflict-safe undo history for unattended memory policy writes.

Records are a small write-ahead journal under the active profile.  A record is
prepared before the memory file is replaced and marked applied only afterwards;
``list_history`` reconciles an interrupted journal record by comparing the
on-disk file to its before/after fingerprints.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from hermes_constants import get_hermes_home


def _history_dir() -> Path:
    return get_hermes_home() / "state" / "memory-history"


def _path(record_id: str) -> Path:
    if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{32}", record_id):
        raise ValueError("Invalid memory history id")
    return _history_dir() / f"{record_id}.json"


def _fingerprint(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_record(record: Dict[str, Any]) -> None:
    path = _path(record["id"])
    from utils import atomic_json_write
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json_write(path, record, mode=0o600, sort_keys=True)


def _read_record(record_id: str) -> Dict[str, Any] | None:
    try:
        return json.loads(_path(record_id).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


class HistoryTransaction:
    """Store callback used while its target lock is held."""

    def __init__(self, target: str, operations: List[Dict[str, Any]]):
        self.target, self.operations, self.record = target, operations, None

    def prepare(self, path: Path, before_raw: str, after_entries: List[str]) -> str:
        record_id = uuid.uuid4().hex
        after_raw = "\n§\n".join(after_entries)
        self.record = {
            "id": record_id,
            "target": self.target,
            "operations": self.operations,
            "before": before_raw,
            "after": after_raw,
            "before_sha256": _fingerprint(before_raw),
            "after_sha256": _fingerprint(after_raw),
            "status": "prepared",
            "created_at": time.time(),
        }
        _write_record(self.record)
        return record_id

    def applied(self) -> None:
        if self.record is None:
            return
        self.record["status"] = "applied"
        self.record["applied_at"] = time.time()
        _write_record(self.record)


def _recover(record: Dict[str, Any]) -> Dict[str, Any]:
    from tools.memory_tool_store import MemoryStore
    with MemoryStore._file_lock(MemoryStore._path_for(record["target"])):
        return _recover_locked(_read_record(record["id"]) or record)


def _recover_locked(record: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a crash-window record without guessing about third-party writes."""
    if record.get("status") != "prepared":
        return record
    try:
        from tools.memory_tool_store import MemoryStore
        raw, readable = MemoryStore._read_raw_checked(MemoryStore._path_for(record["target"]))
    except Exception:
        return record
    if not readable:
        return record
    digest = _fingerprint(raw)
    if digest == record.get("after_sha256"):
        record["status"] = "applied"
        record["recovered_at"] = time.time()
        _write_record(record)
    elif digest == record.get("before_sha256"):
        record["status"] = "not_applied"
        record["recovered_at"] = time.time()
        _write_record(record)
    else:
        record["status"] = "conflict"
        record["recovered_at"] = time.time()
        _write_record(record)
    return record


def list_history() -> List[Dict[str, Any]]:
    """List durable history, reconciling interrupted writes where possible."""
    records = []
    for path in _history_dir().glob("*.json") if _history_dir().exists() else ():
        try:
            records.append(_recover(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(records, key=lambda row: row.get("created_at", 0), reverse=True)


def rollback(record_id: str, store) -> Dict[str, Any]:
    """Restore a history record exactly once; never overwrite newer memory.

    A second successful rollback is idempotent.  Any current state other than
    the recorded after-image is a conflict and is left untouched.
    """
    record = _read_record(record_id)
    if not record:
        return {"success": False, "error": f"Unknown memory history id '{record_id}'."}
    target, path = record["target"], store._path_for(record["target"])
    with store._file_lock(path):
        record = _read_record(record_id)
        if not record or record.get("target") != target:
            return {"success": False, "error": "History record changed before rollback acquired its target."}
        record = _recover_locked(record)
        if record.get("status") == "not_applied":
            return {"success": True, "done": True, "id": record_id, "message": "Write was never applied."}
        if record.get("status") == "rolled_back":
            return {"success": True, "done": True, "id": record_id, "message": "Already rolled back."}
        if record.get("status") != "applied":
            return {"success": False, "error": "History record cannot be rolled back safely.", "status": record.get("status")}
        current, readable = store._read_raw_checked(path)
        if not readable:
            return {"success": False, "error": "Memory file could not be read; rollback was not applied."}
        current_sha = _fingerprint(current)
        if current_sha == record["before_sha256"]:
            record["status"] = "rolled_back"
            record["rolled_back_at"] = time.time()
            _write_record(record)
            return {"success": True, "done": True, "id": record_id, "message": "Already rolled back."}
        if current_sha != record["after_sha256"]:
            return {"success": False, "error": "Memory changed after this history entry; rollback refused.", "conflict": True}
        before_entries = store._parse_entries(record["before"])
        store._write_file(path, before_entries)
        store._set_entries(target, before_entries)
        record["status"] = "rolled_back"
        record["rolled_back_at"] = time.time()
        _write_record(record)
    return {"success": True, "done": True, "id": record_id, "message": "Rolled back."}


def main():
    """Operator-only recovery CLI; not exposed to review forks as a model tool."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    undo = commands.add_parser("rollback")
    undo.add_argument("id")
    args = parser.parse_args()
    if args.command == "list":
        result = list_history()
    else:
        from tools.memory_tool_store import MemoryStore
        store = MemoryStore()
        store.load_from_disk()
        result = rollback(args.id, store)
    print(json.dumps(result, ensure_ascii=False))
    if isinstance(result, dict) and not result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
