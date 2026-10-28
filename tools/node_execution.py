"""Call-local named-node execution, using the shared addressing registry.

The raw task identity remains available to hooks and process ownership. Only
mutable terminal/file state uses the profile/session/connection-qualified key.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import logging
import threading

from tools.nodes import Node, NodeError, resolve_node


@dataclass(frozen=True)
class Execution:
    node: Node
    key: str
    task_id: str


_execution: ContextVar[Execution | None] = ContextVar("node_execution", default=None)
_locks: dict[str, tuple[threading.RLock, int]] = {}
_lock = threading.Lock()


def current_execution() -> Execution | None:
    return _execution.get()


def tracking_key(task_id: str) -> str:
    execution = current_execution()
    return f"{execution.key}:task:{task_id}" if execution else task_id


def path_key(path: str) -> str:
    execution = current_execution()
    return json.dumps([execution.key, path]) if execution else path


def node_schema() -> dict:
    return {"type": "string", "description": "Named execution node from nodes.hosts. Omit to use nodes.default, or the legacy backend when no default is configured. Unknown or unavailable nodes never fall back."}


def node_handler(handler):
    """Bind a single destination before entering any terminal/file implementation."""
    @wraps(handler)
    def run(args, **kw):
        selected = None
        try:
            from tools.terminal_scope import enforce_no_refusal
            enforce_no_refusal()
            requested = args.get("node")
            if "node" in args and (not isinstance(requested, str) or not requested.strip()):
                raise NodeError("node must be a non-empty configured node name")
            selected = resolve_node(requested)
            if selected is None:
                return handler(args, **kw)
            if selected.platform not in {"macos", "linux"}:
                raise NodeError("Named terminal/file execution currently requires a macos or linux SSH target")
            from hermes_constants import get_hermes_home
            from tools.approval import get_current_session_key
            from tools.terminal_tool import resolve_task_overrides
            raw = kw.get("task_id") or "default"
            overrides = resolve_task_overrides(raw)
            if overrides.get("env_type") not in (None, "ssh") or any(
                    key.endswith("_image") for key in overrides):
                raise NodeError("Named node conflicts with this task's sandbox override")
            if args.get("pty"):
                raise NodeError("PTY is not supported for named-node SSH execution, no command was spawned")
            session = get_current_session_key(default="") or kw.get("session_id") or raw
            identity = json.dumps([str(get_hermes_home()), session, selected.name, selected.ssh, selected.platform])
            key = "node:" + hashlib.sha256(identity.encode()).hexdigest()
            execution = Execution(selected, key, raw)
            with _lock:
                mutex, users = _locks.get(key, (threading.RLock(), 0))
                _locks[key] = (mutex, users + 1)
            try:
                with mutex:
                    token = _execution.set(execution)
                    try:
                        result = handler(args, **kw)
                    finally:
                        _execution.reset(token)
            finally:
                with _lock:
                    mutex, users = _locks[key]
                    if users == 1:
                        del _locks[key]
                    else:
                        _locks[key] = (mutex, users - 1)
            data = json.loads(result)
            data.update(node=selected.name, ssh=selected.ssh)
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            logging.getLogger(__name__).debug("Named-node call refused or failed", exc_info=True)
            from tools.registry import tool_error
            return tool_error(str(exc), node=selected.name if selected else args.get("node"),
                              **({"ssh": selected.ssh} if selected else {}))
    return run
