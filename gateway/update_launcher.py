"""Gateway-side admission and launch boundary for native update requests."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_AGENT_UPDATE_REASON = 240
_UPDATE_HANDOFF = "Update accepted. End this turn now; the native updater owns completion."
_MAX_PARENT_DEPTH = 32


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
    home = Path(home)
    pending_path = home / ".update_pending.json"
    claimed_path = home / ".update_pending.claimed.json"
    output_path = home / ".update_output.txt"
    exit_code_path = home / ".update_exit_code"
    if claimed_path.exists():
        return {"started": False, "pending": True}
    pending = {**pending, "notification_version": 2}
    encoded = json.dumps(pending).encode("utf-8")
    try:
        fd = os.open(str(pending_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"started": False, "pending": True}
    try:
        with os.fdopen(fd, "wb") as marker:
            marker.write(encoded)
            marker.flush()
            os.fsync(marker.fileno())
        # A prior updater may have claimed between the first check and our O_EXCL.
        # Remove only our new pending marker; preserve the claimed request verbatim.
        if claimed_path.exists():
            pending_path.unlink(missing_ok=True)
            return {"started": False, "pending": True}
        exit_code_path.unlink(missing_ok=True)
        (home / ".update_process_exit_code").unlink(missing_ok=True)
        spawn(hermes_cmd, output_path, exit_code_path)
    except Exception:
        pending_path.unlink(missing_ok=True)
        exit_code_path.unlink(missing_ok=True)
        raise
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
        if not (Path(__file__).parent.parent.resolve() / ".git").exists():
            return {"accepted": False, "error": "update requires a git checkout"}
        try:
            resolved = _runner_session_lineage(runner, home, session_id)
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
        hermes_cmd = resolve_hermes_bin()
        if not hermes_cmd:
            return {"accepted": False, "error": "Hermes command unavailable"}
        pending = {
            "platform": route["source"], "chat_id": route["chat_id"],
            "chat_type": route.get("chat_type"), "user_id": route.get("user_id"),
            # The validated agent:<profile>: lane lets _marker_profile retain the owning bot.
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


def _runner_session_lineage(runner: Any, home: Path, session_id: str):
    """Resolve only gateway-owned stores and refuse ambiguous cross-profile IDs."""
    if not getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        return _route_from_session_lineage(runner._session_db._db, session_id)
    from gateway.run import _multiplex_profile_homes
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    scopes = [("default", home), *_multiplex_profile_homes(runner.config)]
    seen = set()
    matches = []
    for profile, candidate_home in scopes:
        candidate_home = Path(candidate_home).resolve()
        if candidate_home in seen:
            continue
        seen.add(candidate_home)
        token = set_hermes_home_override(candidate_home)
        try:
            db = runner._session_db._db
            if db.get_session(session_id) is None:
                continue
            route = _route_from_session_lineage(db, session_id)
            if route is None:
                return None
            parts = str(route[1].get("session_key") or "").split(":")
            expected = "main" if profile == "default" else profile
            if len(parts) < 5 or parts[:2] != ["agent", expected]:
                return None
            matches.append(route)
        finally:
            reset_hermes_home_override(token)
    return matches[0] if len(matches) == 1 else None
