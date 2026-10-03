"""Gateway-side admission and launch boundary for native update requests."""

from __future__ import annotations

import json
import os
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_AGENT_UPDATE_REASON = 240
_UPDATE_HANDOFF = "Update accepted. End this turn now; the native updater owns completion."
_MAX_PARENT_DEPTH = 32
_MAX_PREVIOUS_OUTCOME_DETAIL = 240
_MAX_PREVIOUS_OUTCOME_COUNT = 10000


def _bounded_previous_detail(value: object) -> str:
    detail = str(value or "").strip()
    if len(detail) <= _MAX_PREVIOUS_OUTCOME_DETAIL:
        return detail
    return detail[:_MAX_PREVIOUS_OUTCOME_DETAIL - 1].rstrip() + "…"


def _previous_outcome_older_count(old: dict[str, Any]) -> int:
    """Count outcomes older than the immediate prior result without retaining their payloads."""
    previous = old.get("previous_outcome")
    if not isinstance(previous, dict):
        return 0
    declared = old.get("previous_outcome_older_count")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared >= 0:
        return min(_MAX_PREVIOUS_OUTCOME_COUNT, declared + 1)
    count = 0
    while isinstance(previous, dict) and count < _MAX_PREVIOUS_OUTCOME_COUNT:
        count += 1
        previous = previous.get("previous_outcome")
    return count


def validate_agent_update_reason(reason: object) -> str:
    """Return a bounded single-paragraph reason before any IPC or state mutation."""
    if not isinstance(reason, str):
        raise ValueError("--reason is required")
    cleaned = reason.strip()
    if not cleaned:
        raise ValueError("--reason must not be blank")
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("--reason must be a single paragraph")
    if len(cleaned) > MAX_AGENT_UPDATE_REASON:
        raise ValueError(f"--reason must be at most {MAX_AGENT_UPDATE_REASON} characters")
    return cleaned


def launch_native_update(
    *, home: Path, hermes_cmd: list[str], pending: dict[str, Any], spawn: Callable[[list[str], Path, Path], None],
) -> dict[str, Any]:
    """Persist one pending route and detach exactly one native updater.

    A claimed marker is still an active admission marker: it is the pending file
    after the updater atomically takes ownership.  Do not overwrite its reason.
    """
    from gateway.update_notifications import final_outcome, locked_update_marker, read_pending

    home = Path(home)
    pending_path = home / ".update_pending.json"
    claimed_path = home / ".update_pending.claimed.json"
    output_path = home / ".update_output.txt"
    exit_code_path = home / ".update_exit_code"
    with locked_update_marker(home):
        if claimed_path.exists():
            return {"started": False, "pending": True}
        previous = None
        previous_outcome_older_count = 0
        old_bytes = None
        old_process_exit = None
        lifecycle_paths = (
            output_path,
            home / ".update_prompt.json",
            home / ".update_response",
        )
        lifecycle_artifacts: dict[Path, bytes | None] = {}
        if pending_path.exists():
            old = read_pending(home)
            if not old or old[0] != pending_path or old[1].get("notification_version") != 2:
                return {"started": False, "pending": True}
            outcome = final_outcome(home, old[1])
            # Only the actual process-exit sentinel permits reuse; a pre-restart exit
            # receipt or stale receipt alone never releases an active admission.
            if outcome is None or not (home / ".update_process_exit_code").exists() or claimed_path.exists():
                return {"started": False, "pending": True}
            previous = {"success": outcome[0], "detail": _bounded_previous_detail(outcome[1]),
                        "reason": old[1].get("reason"), "timestamp": old[1].get("timestamp")}
            previous_outcome_older_count = _previous_outcome_older_count(old[1])
            old_bytes = pending_path.read_bytes()
            old_process_exit = (home / ".update_process_exit_code").read_bytes()
        pending = {**pending, "notification_version": 2, "request_id": uuid4().hex}
        pending.pop("output_offset", None)
        if previous is not None:
            pending["previous_outcome"] = previous
            if previous_outcome_older_count:
                pending["previous_outcome_older_count"] = previous_outcome_older_count
        encoded = json.dumps(pending).encode("utf-8")
        temporary = home / f".update_pending.{uuid4().hex}.tmp" if old_bytes is not None else pending_path
        for path in lifecycle_paths:
            lifecycle_artifacts[path] = path.read_bytes() if path.exists() else None
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return {"started": False, "pending": True}
        try:
            with os.fdopen(fd, "wb") as marker:
                marker.write(encoded)
                marker.flush()
                os.fsync(marker.fileno())
            if old_bytes is not None:
                if claimed_path.exists():
                    return {"started": False, "pending": True}
                os.replace(temporary, pending_path)
            # A prior updater may claim after the first check; never supersede its claim.
            if claimed_path.exists():
                pending_path.unlink(missing_ok=True)
                return {"started": False, "pending": True}
            exit_code_path.unlink(missing_ok=True)
            (home / ".update_process_exit_code").unlink(missing_ok=True)
            # The marker is durable and the watcher is armed as soon as spawn returns;
            # stale lifecycle files must not be attributed to this request.
            for path in lifecycle_paths:
                path.unlink(missing_ok=True)
            spawn(hermes_cmd, output_path, exit_code_path)
        except Exception:
            if old_bytes is not None and not claimed_path.exists():
                temporary.write_bytes(old_bytes)
                os.replace(temporary, pending_path)
                if old_process_exit is not None:
                    (home / ".update_process_exit_code").write_bytes(old_process_exit)
            else:
                pending_path.unlink(missing_ok=True)
            for path, content in lifecycle_artifacts.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            exit_code_path.unlink(missing_ok=True)
            raise
        finally:
            if temporary != pending_path:
                temporary.unlink(missing_ok=True)
    return {"started": True, "pending": False}


def _route_from_session_lineage(db: Any, session_id: str) -> tuple[str, dict[str, Any]] | None:
    """Walk a direct or delegated session to its persisted messaging origin."""
    from gateway.config import Platform

    current = session_id
    seen: set[str] = set()
    for _ in range(_MAX_PARENT_DEPTH):
        if not current or current in seen:
            return None
        seen.add(current)
        row = db.get_session(current)
        if not isinstance(row, dict):
            return None
        source = str(row.get("source") or "").strip()
        chat_id = row.get("chat_id")
        if source and chat_id not in (None, ""):
            try:
                Platform(source)
            except ValueError:
                return None
            route = {
                key: row[key]
                for key in ("source", "chat_id", "chat_type", "user_id", "session_key", "thread_id")
                if row.get(key) not in (None, "")
            }
            return current, route
        current = str(row.get("parent_session_id") or "").strip()
    return None


def make_agent_update_handler(
    *, runner: Any, home: Path, main_loop: Any, resolve_hermes_bin: Callable[[], list[str] | None],
    spawn: Callable[[list[str], Path, Path], None], is_managed: Callable[[], bool],
) -> Callable[[object], dict[str, Any]]:
    """Build the synchronous socket handler; scheduling is always marshalled to its loop."""
    home = Path(home)

    def _handler(payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"accepted": False, "error": "invalid update request"}
        try:
            reason = validate_agent_update_reason(payload.get("reason"))
        except ValueError as exc:
            return {"accepted": False, "error": str(exc)}
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return {"accepted": False, "error": "no session route"}
        if is_managed():
            return {"accepted": False, "error": "managed installs cannot update here"}
        try:
            hermes_cmd = resolve_hermes_bin()
        except (RuntimeError, OSError, ValueError) as exc:
            return {"accepted": False, "error": str(exc)}
        if not hermes_cmd:
            return {"accepted": False, "error": "update requires a git checkout"}
        try:
            db = runner._session_db._db
            resolved = _route_from_session_lineage(db, session_id)
        except Exception:
            resolved = None
        if resolved is None:
            return {"accepted": False, "error": "no deliverable messaging session route"}
        parent_session_id, route = resolved
        from gateway.config import Platform
        from gateway.run import GatewayRunner
        platform = Platform(route["source"])
        if platform not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(platform.value)
            if not entry or not entry.allow_update_command:
                return {"accepted": False, "error": "update requires a messaging session"}
        pending = {
            "platform": route["source"], "chat_id": route["chat_id"],
            "chat_type": route.get("chat_type"), "user_id": route.get("user_id"),
            "session_key": route.get("session_key"), "thread_id": route.get("thread_id"),
            "timestamp": datetime.now(timezone.utc).isoformat(), "reason": reason,
            "parent_session_id": parent_session_id, "parent_route": route,
        }
        try:
            result = launch_native_update(home=home, hermes_cmd=hermes_cmd, pending=pending, spawn=spawn)
        except Exception:
            return {"accepted": False, "error": "native updater could not start"}
        if result["started"]:
            # Socket handlers run in an executor; the watcher creates asyncio tasks.
            main_loop.call_soon_threadsafe(runner._schedule_update_notification_watch)
        return {"accepted": True, **result, "handoff": _UPDATE_HANDOFF}

    return _handler
