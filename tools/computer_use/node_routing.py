"""Per-call node routing. No selection is sticky or written into the model prompt."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import time

from tools.nodes import Node, NodeError, resolve_node

_node: ContextVar[Node | None] = ContextVar("computer_use_node", default=None)
_deadline: ContextVar[float | None] = ContextVar("computer_use_deadline", default=None)


def current_node() -> Node | None:
    return _node.get()


def remaining(limit: float = 8.0) -> float:
    deadline = _deadline.get()
    value = limit if deadline is None else min(limit, deadline - time.monotonic())
    if value <= 0:
        raise TimeoutError("Remote node deadline exceeded. No local or sibling fallback was attempted")
    return value


@contextmanager
def node_scope(node: Node):
    token, deadline = _node.set(node), _deadline.set(time.monotonic() + 8.0)
    try:
        yield
    finally:
        _deadline.reset(deadline)
        _node.reset(token)


def cache_key(session_id: str, local_key: str) -> str | tuple[str, str, Node]:
    node = current_node()
    if node is None:
        return local_key
    from hermes_constants import hermes_home_key
    return (hermes_home_key(), session_id, node)


def owns_remote_key(key: object, session_id: str) -> bool:
    from hermes_constants import hermes_home_key
    return isinstance(key, tuple) and key[:2] == (hermes_home_key(), session_id)


def _tag(result, target: str):
    if isinstance(result, dict) and result.get("_multimodal"):
        result["node"] = target
        label = f"Node: {target}\n"
        result["text_summary"] = label + result["text_summary"]
        for part in result["content"]:
            if part.get("type") == "text":
                part["text"] = label + part["text"]
                break
        else:
            result["content"].insert(0, {"type": "text", "text": label.strip()})
        return result
    payload = result if isinstance(result, dict) else json.loads(result)
    return json.dumps({**payload, "node": target})


def handle_on_node(args, handler, **kwargs):
    target = args.get("node")
    try:
        node = resolve_node(target)
        target = node.name if node else "local"
        if node is None:
            return _tag(handler(args, **kwargs), target)
        from plugins.computer_use import configured_backend_name
        if configured_backend_name() != "cua":
            raise NodeError("Named nodes require computer_use.backend: cua")
        with node_scope(node):
            return _tag(handler(args, **kwargs), target)
    except Exception as exc:
        return json.dumps({"ok": False, "node": target, "error": str(exc), "code": "node_unavailable"})
