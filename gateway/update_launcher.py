"""Gateway-side admission and launch boundary for native update requests."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_AGENT_UPDATE_REASON = 240
_UPDATE_HANDOFF = "Update request accepted; update is not complete. End this turn now; the native updater owns completion."
_MAX_PARENT_DEPTH = 32


class UpdateChildNotStarted(RuntimeError):
    """The native process-creation boundary proved no updater was started."""


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
    *, home: Path, hermes_cmd: list[str], pending: dict[str, Any], spawn: Callable[..., None],
    revision: str | None = None,
) -> dict[str, Any]:
    """Persist one pending route and detach exactly one native updater.

    A legacy claimed marker remains authoritative for admission. Current native
    launch and notification paths do not rename pending markers to claimed.
    Do not overwrite either marker's reason.
    """
    from gateway.status import _release_file_lock, _try_acquire_file_lock

    if revision is not None:
        from hermes_cli.update_revision import validate_revision
        revision = validate_revision(revision)
        pending = {**pending, "revision": revision}
    home = Path(home)
    pending_path = home / ".update_pending.json"
    claimed_path = home / ".update_pending.claimed.json"
    output_path = home / ".update_output.txt"
    exit_code_path = home / ".update_exit_code"
    staging_path = home / ".update_pending.initializing"
    if pending_path.exists() or claimed_path.exists():
        return {"started": False, "pending": True}
    pending = {**pending, "notification_version": 2}
    encoded = json.dumps(pending).encode("utf-8")
    # Keep a stable lock inode: the kernel, not a PID/age guess, owns initializer
    # liveness. Never unlink this file, including after an interrupted initializer.
    with (home / ".update_admission.lock").open("a+", encoding="utf-8") as lock:
        if not _try_acquire_file_lock(lock):
            return {"started": False, "pending": True}
        try:
            if pending_path.exists() or claimed_path.exists():
                return {"started": False, "pending": True}
            # Staging is never a request. Only the lock owner can replace remnants
            # from a dead initializer; existing pending/claimed files fail closed.
            fd = os.open(str(staging_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as marker:
                with output_path.open("wb") as output:
                    output.flush()
                    os.fsync(output.fileno())
                exit_code_path.unlink(missing_ok=True)
                (home / ".update_process_exit_code").unlink(missing_ok=True)
                marker.write(encoded)
                marker.flush()
                os.fsync(marker.fileno())
            os.replace(staging_path, pending_path)
            published = pending_path.stat()
            # Publication is the uncertainty fence. Only a classified failure at
            # the actual process-creation boundary can retract our exact marker.
            try:
                if revision is None:
                    spawn(hermes_cmd, output_path, exit_code_path)
                else:
                    spawn(hermes_cmd, output_path, exit_code_path, revision=revision)
            except UpdateChildNotStarted:
                if not claimed_path.exists():
                    try:
                        current = pending_path.stat()
                        if ((current.st_dev, current.st_ino) == (published.st_dev, published.st_ino)
                                and pending_path.read_bytes() == encoded):
                            pending_path.unlink()
                    except FileNotFoundError:
                        pass
                raise
        finally:
            _release_file_lock(lock)
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
            # Rich routing is persisted in origin_json, not flat session columns.
            # Accept a Business discriminator only from this same chat origin.
            try:
                origin = json.loads(row.get("origin_json") or "{}")
            except (TypeError, ValueError):
                origin = {}
            if (isinstance(origin, dict) and source == "telegram"
                    and origin.get("platform") == source
                    and str(origin.get("chat_id")) == str(chat_id)
                    and isinstance(origin.get("business_connection_id"), str)
                    and origin["business_connection_id"].strip()):
                route["business_connection_id"] = origin["business_connection_id"].strip()
            return current, route
        current = str(row.get("parent_session_id") or "").strip()
    return None


def make_agent_update_handler(
    *, runner: Any, home: Path, main_loop: Any, resolve_hermes_bin: Callable[[], list[str] | None],
    spawn: Callable[..., None], is_managed: Callable[[], bool],
) -> Callable[[object], dict[str, Any]]:
    """Build the synchronous socket handler; scheduling is always marshalled to its loop."""
    home = Path(home)

    def _handler(payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"accepted": False, "error": "invalid update request"}
        try:
            reason = validate_agent_update_reason(payload.get("reason"))
            revision = payload.get("revision")
            if revision is not None:
                from hermes_cli.update_revision import validate_revision
                revision = validate_revision(revision)
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
            "business_connection_id": route.get("business_connection_id"),
            "timestamp": datetime.now(timezone.utc).isoformat(), "reason": reason,
            "parent_session_id": parent_session_id, "parent_route": route,
        }
        try:
            result = launch_native_update(home=home, hermes_cmd=hermes_cmd, pending=pending,
                                          spawn=spawn, revision=revision)
        except Exception:
            return {"accepted": False, "error": "native updater handoff failed; outcome may be unknown. "
                    "Inspect pending state before retrying"}
        if result["started"]:
            # Socket handlers run in an executor; the watcher creates asyncio tasks.
            main_loop.call_soon_threadsafe(runner._schedule_update_notification_watch)
        else:
            return {"accepted": False, **result,
                    "error": "another update is already pending; this request was not accepted"}
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
