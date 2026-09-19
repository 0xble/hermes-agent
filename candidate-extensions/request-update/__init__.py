"""Parent-only bridge to Hermes's native update watcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime
from typing import Any

_SCHEMA = {
    "name": "request_update",
    "description": "Request the native Hermes update flow for a concrete reason. The update check is read-only; when an update exists, the gateway-owned detached watcher performs the normal update and restart.",
    "parameters": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "Why the update is needed."}},
        "required": ["reason"],
    },
}


def _json(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _session_value(name: str, kwargs: dict[str, Any]) -> str:
    """Read gateway routing context without making the model supply it."""
    value = str(kwargs.get(name) or "").strip()
    if value:
        return value
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(f"HERMES_SESSION_{name.upper()}", "") or "").strip()
    except Exception:
        return ""


def _is_child() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context
        return bool(is_delegated_child_context())
    except Exception:
        return False


def request_update(args: dict[str, Any], **kwargs: Any) -> str:
    if _is_child():
        return _json(success=False, status="refused", error_code="parent_only",
                     error="request_update is available only to the owning parent session")
    reason = str(args.get("reason") or "").strip() if isinstance(args, dict) else ""
    if not reason:
        return _json(success=False, status="refused", error_code="reason_required",
                     error="request_update requires a non-empty reason")
    home = _home()
    pending = home / ".update_pending.json"
    if pending.exists():
        return _json(success=False, status="refused", error_code="update_pending",
                     error="an update request is already pending")
    check = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "update", "--check"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        env=os.environ.copy(), check=False,
    )
    output = f"{check.stdout}\n{check.stderr}".strip()
    if check.returncode != 0:
        return _json(success=False, status="refused", error_code="update_check_failed",
                     error=output[-2000:])
    if "already up to date" in output.casefold():
        return _json(success=False, status="refused", error_code="no_update",
                     error="the native update check found no update")
    output = home / ".update_output.txt"
    exit_code = home / ".update_exit_code"
    pending_data = {
        "platform": _session_value("platform", kwargs),
        "chat_id": _session_value("chat_id", kwargs),
        "chat_type": _session_value("chat_type", kwargs),
        "user_id": _session_value("user_id", kwargs),
        "session_key": _session_value("key", kwargs) or _session_value("session_key", kwargs),
        "profile": _session_value("profile", kwargs),
        "thread_id": _session_value("thread_id", kwargs),
        "message_id": _session_value("message_id", kwargs),
        "reason": reason,
        "source": "request_update",
        "timestamp": datetime.now().isoformat(),
    }
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({k: v for k, v in pending_data.items() if v}) + "\n", encoding="utf-8")
    exit_code.unlink(missing_ok=True)
    (home / ".update_prompt.json").unlink(missing_ok=True)
    (home / ".update_response").unlink(missing_ok=True)
    try:
        from gateway.run import _resolve_hermes_bin
        from gateway.slash_commands import _spawn_detached_update
        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            raise RuntimeError("Hermes executable could not be resolved")
        _spawn_detached_update(hermes_cmd, output, exit_code)
    except Exception as exc:
        pending.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        exit_code.unlink(missing_ok=True)
        return _json(success=False, status="refused", error_code="spawn_failed", error=str(exc))
    # The slash command arms the gateway's completion watcher after spawning; without that, progress
    # and prompts are only picked up if the gateway restarts under the update. Tools run inside the
    # gateway process, so arm it the same way through the runner reference pairing.py already uses.
    watcher = _arm_update_watcher()
    return _json(success=True, status="accepted", reason=reason, watcher=watcher,
                 routed=bool(pending_data.get("platform") and pending_data.get("chat_id")))


def _arm_update_watcher() -> str:
    """Ask the running gateway to watch this update; returns what happened, never raises."""
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        if runner is None:
            return "no_gateway"
        schedule = getattr(runner, "_schedule_update_notification_watch", None)
        if not callable(schedule):
            return "unsupported"
        # Tools execute on an executor thread, not the gateway's event loop, so the schedule call
        # (which creates an asyncio task) must be handed to the loop the runner recorded at start.
        loop = getattr(runner, "_gateway_loop", None)
        if loop is None or not hasattr(loop, "call_soon_threadsafe"):
            return "no_loop"
        loop.call_soon_threadsafe(schedule)
        return "armed"
    except Exception as exc:
        return f"error:{type(exc).__name__}"


def register(ctx: Any) -> None:
    ctx.register_tool(name="request_update", toolset="request_update",
                      schema=_SCHEMA, handler=request_update)
