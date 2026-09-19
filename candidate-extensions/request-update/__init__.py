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
    "description": (
        "Request the native Hermes update flow for a concrete reason. The update check is read-only;",
        "when an update exists, the gateway-owned detached watcher performs the normal update and restart.",
    ),
    "parameters": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "Why the update is needed."}},
        "required": ["reason"],
    },
}


def _json(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def _pending_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / ".update_pending.json"


def _is_child() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context
        return bool(is_delegated_child_context())
    except Exception:
        return False


def request_update(args: dict[str, Any], **_: Any) -> str:
    if _is_child():
        return _json(success=False, status="refused", error_code="parent_only",
                     error="request_update is available only to the owning parent session")
    reason = str(args.get("reason") or "").strip() if isinstance(args, dict) else ""
    if not reason:
        return _json(success=False, status="refused", error_code="reason_required",
                     error="request_update requires a non-empty reason")
    pending = _pending_path()
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
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"reason": reason, "source": "request_update",
                                   "timestamp": datetime.now().isoformat()}) + "\n",
                       encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "update", "--gateway"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, env=os.environ.copy(),
        )
    except Exception as exc:
        pending.unlink(missing_ok=True)
        return _json(success=False, status="refused", error_code="spawn_failed", error=str(exc))
    return _json(success=True, status="accepted", pid=process.pid, reason=reason)


def register(ctx: Any) -> None:
    ctx.register_tool(name="request_update", toolset="request_update",
                      schema=_SCHEMA, handler=request_update)
