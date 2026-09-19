"""Candidate memory journal for the built-in Hermes memory tool.

Observes every successful ``memory`` write, records the before/after images in a profile-local
hash-chained journal, and offers a safe undo. The journal only observes: a damaged journal makes
``undo`` refuse and ``list`` report the break, while memory writes continue unaffected.

Retention (Fork-Patch slice-5): the journal keeps the last ``_KEEP_RECENT`` entries plus one entry
per day for ``_KEEP_DAYS`` days. Compaction rewrites the file and re-seals the chain from the first
kept entry; the entry that was dropped ahead of it is recorded as the new root's ``compacted_from``
so a reader can see the chain was cut deliberately, not corrupted.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_PENDING: dict[str, dict[str, Any]] = {}
_MEMORY_TOOLS = {"memory"}
_KEEP_RECENT = 200
_KEEP_DAYS = 90
_DAY = 86400.0

_SCHEMA = {
    "name": "memory_undo",
    "description": "Undo the latest successful built-in memory write when the profile has not changed since it. Refuses stale or non-latest undo requests, and refuses when the journal's hash chain is damaged.",
    "parameters": {"type": "object", "properties": {"sequence": {"type": "integer", "description": "Optional latest journal sequence."}}, "required": []},
}
_LIST_SCHEMA = {
    "name": "memory_journal_list",
    "description": "List recent built-in memory write journal entries (sequence, action, target, time, hashes) and report whether the hash chain is intact. Read-only.",
    "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Entries to return, newest first (default 20)."}}, "required": []},
}


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _path(target: str) -> Path:
    return _home() / "memories" / ("USER.md" if target == "user" else "MEMORY.md")


def _journal() -> Path:
    return _home() / "memory-journal.jsonl"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key(task_id: str, call_id: str, target: str) -> str:
    return "|".join((str(task_id or ""), str(call_id or ""), target))


def _seal(entry: dict[str, Any]) -> str:
    """Hash of the entry with its own ``hash`` removed: what ``previous_hash`` must equal."""
    unsigned = {k: v for k, v in entry.items() if k != "hash"}
    return _sha(json.dumps(unsigned, sort_keys=True, separators=(",", ":")))


def _load() -> tuple[list[dict[str, Any]], str | None]:
    """All entries in file order plus a break description, or ``None`` when the chain is intact."""
    path = _journal()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"journal unreadable: {exc}"
    rows: list[dict[str, Any]] = []
    previous_hash = ""
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return rows, f"line {number} is not valid JSON"
        if not isinstance(row, dict) or "hash" not in row:
            return rows, f"line {number} is not a journal entry"
        if row.get("hash") != _seal(row):
            return rows, f"entry {row.get('sequence', number)} hash does not match its content"
        if row.get("previous_hash", "") != previous_hash:
            return rows, f"entry {row.get('sequence', number)} does not chain to its predecessor"
        rows.append(row)
        previous_hash = row["hash"]
    return rows, None


def _last() -> dict[str, Any] | None:
    rows, _ = _load()
    return rows[-1] if rows else None


def _write_rows(rows: list[dict[str, Any]]) -> None:
    path = _journal()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".memory-journal.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    os.chmod(temp_name, 0o600)
    os.replace(temp_name, path)


def _append(entry: dict[str, Any]) -> dict[str, Any]:
    path = _journal()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _last()
    entry["sequence"] = int(previous.get("sequence", 0) + 1) if previous else 1
    entry["previous_hash"] = previous.get("hash", "") if previous else ""
    entry.setdefault("at", time.time())
    entry["hash"] = _seal(entry)
    created = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    if created:
        os.chmod(path, 0o600)
    return entry


def compact(*, keep_recent: int = _KEEP_RECENT, keep_days: int = _KEEP_DAYS, now: float | None = None) -> dict[str, Any]:
    """Apply retention and re-seal the chain. Refuses on a damaged journal (nothing to trust)."""
    with _LOCK:
        rows, damage = _load()
        if damage:
            return {"success": False, "error_code": "journal_damaged", "error": damage}
        if len(rows) <= keep_recent:
            return {"success": True, "kept": len(rows), "dropped": 0}
        now = time.time() if now is None else now
        recent = rows[-keep_recent:]
        older = rows[:-keep_recent]
        cutoff = now - keep_days * _DAY
        kept_days: dict[int, dict[str, Any]] = {}
        for row in older:
            at = float(row.get("at") or 0.0)
            if at < cutoff:
                continue
            kept_days.setdefault(int(at // _DAY), row)  # first entry of each day
        kept = list(kept_days.values()) + recent
        dropped = len(rows) - len(kept)
        if dropped == 0:
            return {"success": True, "kept": len(rows), "dropped": 0}
        # Re-seal: the first kept entry becomes the root; record what preceded it.
        resealed: list[dict[str, Any]] = []
        previous_hash = ""
        first_dropped_before = rows[0]["sequence"] if rows[0] is not kept[0] else None
        for index, row in enumerate(kept):
            row = dict(row)
            if index == 0 and first_dropped_before is not None:
                row["compacted_from"] = first_dropped_before
            row["previous_hash"] = previous_hash
            row["hash"] = _seal(row)
            resealed.append(row)
            previous_hash = row["hash"]
        _write_rows(resealed)
        return {"success": True, "kept": len(resealed), "dropped": dropped}


def _decode(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    try:
        return json.loads(str(result or ""))
    except (TypeError, json.JSONDecodeError):
        return {}


def _on_pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", **kwargs: Any) -> None:
    if tool_name not in _MEMORY_TOOLS or not isinstance(args, dict):
        return
    target = str(args.get("target") or "memory")
    if target not in {"memory", "user"}:
        return
    call_id = str(kwargs.get("tool_call_id") or kwargs.get("call_id") or "")
    key = _key(task_id, call_id, target)
    with _LOCK:
        # A call whose post hook never fires (suppressed, cancelled, raised) would otherwise pin its
        # before-image for the process lifetime. Keep the dict bounded; the oldest entries go first.
        while len(_PENDING) >= 64:
            _PENDING.pop(next(iter(_PENDING)))
        _PENDING[key] = {"target": target, "before": _read(_path(target)),
                         "action": args.get("action") or ("batch" if args.get("operations") else ""),
                         "call_id": call_id, "task_id": task_id}


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, task_id: str = "", **kwargs: Any) -> None:
    if tool_name not in _MEMORY_TOOLS or not isinstance(args, dict):
        return
    target = str(args.get("target") or "memory")
    call_id = str(kwargs.get("tool_call_id") or kwargs.get("call_id") or "")
    key = _key(task_id, call_id, target)
    with _LOCK:
        pending = _PENDING.pop(key, None)
        decoded = _decode(result)
        if not pending or decoded.get("success") is not True or decoded.get("staged") or decoded.get("proposal_staged"):
            return
        after = _read(_path(target))
        if pending["before"] == after:
            return
        _append({"action": pending["action"], "target": target, "before": pending["before"], "after": after,
                 "before_hash": _sha(pending["before"]), "after_hash": _sha(after), "task_id": str(task_id or "")})
        # Bounded growth without a separate scheduler: compaction is cheap and idempotent.
        rows, damage = _load()
        if not damage and len(rows) > _KEEP_RECENT * 2:
            compact()


def memory_undo(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return json.dumps({"success": False, "error_code": "parent_only", "error": "memory_undo is parent-only"})
        with _LOCK:
            rows, damage = _load()
            if damage:
                return json.dumps({"success": False, "error_code": "journal_damaged",
                                   "error": f"memory journal hash chain is damaged ({damage}); refusing to undo from an untrusted history"})
            entry = rows[-1] if rows else None
            if entry is None:
                return json.dumps({"success": False, "error_code": "empty_journal", "error": "memory journal is empty"})
            requested = args.get("sequence")
            if requested is not None and int(requested) != int(entry["sequence"]):
                return json.dumps({"success": False, "error_code": "stale_undo", "error": "only the latest journal entry can be undone"})
            target_path = _path(str(entry["target"]))
            current = _read(target_path)
            if _sha(current) != entry["after_hash"]:
                return json.dumps({"success": False, "error_code": "stale_undo", "error": "memory changed after this journal entry; refusing to overwrite it"})
            target_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=target_path.parent, prefix=f".{target_path.name}.")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(entry["before"])
            os.replace(temp_name, target_path)
            _append({"action": "undo", "target": entry["target"], "before": current, "after": entry["before"],
                     "before_hash": _sha(current), "after_hash": _sha(entry["before"]), "undoes": entry["sequence"],
                     "task_id": str(kwargs.get("task_id") or "")})
            return json.dumps({"success": True, "undid": entry["sequence"], "target": entry["target"]})
    except (TypeError, ValueError, OSError, KeyError) as exc:
        return json.dumps({"success": False, "error_code": "undo_failed", "error": str(exc)})


def memory_journal_list(args: dict[str, Any], **_: Any) -> str:
    try:
        limit = int(args.get("limit") or 20) if isinstance(args, dict) else 20
    except (TypeError, ValueError):
        limit = 20
    with _LOCK:
        rows, damage = _load()
    recent = [{k: row.get(k) for k in ("sequence", "action", "target", "at", "before_hash", "after_hash", "undoes", "compacted_from")
               if k in row} for row in rows[-max(1, limit):]]
    recent.reverse()
    return json.dumps({"success": True, "intact": damage is None, "damage": damage, "entries": len(rows),
                       "verified_entries": len(rows), "recent": recent}, sort_keys=True)


def register(ctx: Any) -> None:
    ctx.register_tool(name="memory_undo", toolset="memory_journal", schema=_SCHEMA, handler=memory_undo)
    ctx.register_tool(name="memory_journal_list", toolset="memory_journal", schema=_LIST_SCHEMA, handler=memory_journal_list)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
