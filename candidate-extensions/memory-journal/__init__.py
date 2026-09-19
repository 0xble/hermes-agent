"""Candidate memory journal for the built-in Hermes memory tool."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_PENDING: dict[str, dict[str, Any]] = {}
_MEMORY_TOOLS = {"memory"}
_SCHEMA = {
    "name": "memory_undo",
    "description": "Undo the latest successful built-in memory write when the profile has not changed since it. Refuses stale or non-latest undo requests.",
    "parameters": {"type": "object", "properties": {"sequence": {"type": "integer", "description": "Optional latest journal sequence."}}, "required": []},
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

def _last() -> dict[str, Any] | None:
    path = _journal()
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return rows[-1] if rows else None

def _append(entry: dict[str, Any]) -> dict[str, Any]:
    path = _journal(); path.parent.mkdir(parents=True, exist_ok=True)
    previous = _last()
    entry["sequence"] = int(previous.get("sequence", 0) + 1) if previous else 1
    entry["previous_hash"] = previous.get("hash", "") if previous else ""
    unsigned = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    entry["hash"] = _sha(unsigned)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    return entry

def _decode(result: Any) -> dict[str, Any]:
    if isinstance(result, dict): return result
    try: return json.loads(str(result or ""))
    except (TypeError, json.JSONDecodeError): return {}

def _on_pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", **kwargs: Any) -> None:
    if tool_name not in _MEMORY_TOOLS or not isinstance(args, dict): return
    target = str(args.get("target") or "memory")
    if target not in {"memory", "user"}: return
    call_id = str(kwargs.get("tool_call_id") or kwargs.get("call_id") or "")
    key = _key(task_id, call_id, target)
    with _LOCK:
        _PENDING[key] = {"target": target, "before": _read(_path(target)), "action": args.get("action") or ("batch" if args.get("operations") else ""), "call_id": call_id, "task_id": task_id}

def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, task_id: str = "", **kwargs: Any) -> None:
    if tool_name not in _MEMORY_TOOLS or not isinstance(args, dict): return
    target = str(args.get("target") or "memory")
    call_id = str(kwargs.get("tool_call_id") or kwargs.get("call_id") or "")
    key = _key(task_id, call_id, target)
    with _LOCK:
        pending = _PENDING.pop(key, None)
        decoded = _decode(result)
        if not pending or decoded.get("success") is not True or decoded.get("staged") or decoded.get("proposal_staged"): return
        after = _read(_path(target))
        if pending["before"] == after: return
        _append({"action": pending["action"], "target": target, "before": pending["before"], "after": after, "before_hash": _sha(pending["before"]), "after_hash": _sha(after), "task_id": str(task_id or "")})

def memory_undo(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return json.dumps({"success": False, "error_code": "parent_only", "error": "memory_undo is parent-only"})
        with _LOCK:
            entry = _last()
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
            with os.fdopen(fd, "w", encoding="utf-8") as handle: handle.write(entry["before"])
            os.replace(temp_name, target_path)
            _append({"action": "undo", "target": entry["target"], "before": current, "after": entry["before"], "before_hash": _sha(current), "after_hash": _sha(entry["before"]), "undoes": entry["sequence"], "task_id": str(kwargs.get("task_id") or "")})
            return json.dumps({"success": True, "undid": entry["sequence"], "target": entry["target"]})
    except (TypeError, ValueError, OSError, KeyError) as exc:
        return json.dumps({"success": False, "error_code": "undo_failed", "error": str(exc)})

def register(ctx: Any) -> None:
    ctx.register_tool(name="memory_undo", toolset="memory_journal", schema=_SCHEMA, handler=memory_undo)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)

