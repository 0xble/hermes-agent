"""Gateway-side admission and launch boundary for native update requests."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import time
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
MAX_AGENT_UPDATE_REASON = 240
_UPDATE_HANDOFF = "Update accepted. End this turn now; the native updater owns completion."
# The deferred file keeps one route (latest requester wins), so an earlier merged requester is
# told up front that the completion notice goes to whoever asked last.
_UPDATE_COALESCED_HANDOFF = ("Update queued with a burst of requests; Hermes restarts once with the latest "
                             "requested target and reports the outcome to the latest requester only. "
                             "End this turn now.")
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
    *, home: Path, hermes_cmd: list[str], pending: dict[str, Any], spawn: Callable[..., None],
    coalesce_window: float | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Persist one pending route and detach exactly one native updater.

    A claimed marker is still an active admission marker: it is the pending file
    after the updater atomically takes ownership.  Do not overwrite its reason.

    With a positive ``coalesce_window`` (seconds; None or 0 keeps the plain refusal), a request that arrives while an install is
    in flight, or within the window after the last launch, is deferred into
    ``.update_deferred.json`` (latest target wins) instead of refused, so a burst becomes one
    gateway restart. Gateway housekeeping launches it through ``launch_deferred_update``. A
    lone request still launches immediately.
    """
    from gateway.update_notifications import locked_update_marker

    home = Path(home)
    now = time.time() if now is None else now
    # The admission primitive keeps its exclusive contract unless a caller opts in: the gateway
    # entry points (``/update``, the agent-update socket) pass ``coalesce_window_seconds()``.
    window = 0.0 if coalesce_window is None else max(0.0, float(coalesce_window))
    with locked_update_marker(home):
        last = _last_launch(home)
        # A queued request or a launch inside the window means a burst: join it instead of
        # restarting again. Outside a burst this is the unchanged immediate launch.
        if window > 0 and ((home / DEFERRED_FILE).exists() or (last is not None and now < last + window)):
            duplicate = _inflight_duplicate(home, pending)
            return {"started": False, "pending": True} if duplicate else _defer_locked(home, pending, now, window)
        result = _launch_locked(home, hermes_cmd, pending, spawn)
        busy = result.pop("busy", None)
        if busy is None:
            _record_launch(home, now)
            return result
        if window <= 0 or busy == "duplicate":
            return result
        return _defer_locked(home, pending, now, window)


def _inflight_duplicate(home: Path, pending: dict[str, Any]) -> bool:
    from gateway.update_notifications import final_outcome, read_pending
    current = read_pending(home)
    if current is None or (current[0].name != ".update_pending.claimed.json"
                           and final_outcome(home, current[1]) is not None):
        return False
    return (current[1].get("reason") == pending.get("reason")
            and current[1].get("branch") == pending.get("branch"))


def _launch_locked(home: Path, hermes_cmd: list[str], pending: dict[str, Any],
                   spawn: Callable[..., None], update_args: list[str] | None = None) -> dict[str, Any]:
    """Admission under ``locked_update_marker``. A refused result carries ``busy``."""
    from gateway.update_notifications import final_outcome, read_pending

    pending_path = home / ".update_pending.json"
    claimed_path = home / ".update_pending.claimed.json"
    output_path = home / ".update_output.txt"
    exit_code_path = home / ".update_exit_code"
    refused = {"started": False, "pending": True, "busy": "inflight"}
    if claimed_path.exists():
        return refused
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
            return refused
        outcome = final_outcome(home, old[1])
        # Only the actual process-exit sentinel permits reuse; a pre-restart exit
        # receipt or stale receipt alone never releases an active admission.
        if outcome is None or not (home / ".update_process_exit_code").exists() or claimed_path.exists():
            # A retry of the in-flight request is already covered by it; never queue it again.
            same = (old[1].get("reason") == pending.get("reason")
                    and old[1].get("branch") == pending.get("branch"))
            return {**refused, "busy": "duplicate" if same else "inflight"}
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
        return refused
    try:
        with os.fdopen(fd, "wb") as marker:
            marker.write(encoded)
            marker.flush()
            os.fsync(marker.fileno())
        if old_bytes is not None:
            if claimed_path.exists():
                return refused
            os.replace(temporary, pending_path)
        # A prior updater may claim after the first check; never supersede its claim.
        if claimed_path.exists():
            pending_path.unlink(missing_ok=True)
            return refused
        exit_code_path.unlink(missing_ok=True)
        (home / ".update_process_exit_code").unlink(missing_ok=True)
        # The marker is durable and the watcher is armed as soon as spawn returns;
        # stale lifecycle files must not be attributed to this request.
        for path in lifecycle_paths:
            path.unlink(missing_ok=True)
        if update_args is None:
            spawn(hermes_cmd, output_path, exit_code_path)
        else:
            spawn(hermes_cmd, output_path, exit_code_path, update_args=update_args)
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


# ---- coalescing (state files only; no queue service) ------------------------------------------

DEFERRED_FILE = ".update_deferred.json"
LAST_LAUNCH_FILE = ".update_last_launch.json"
DEFAULT_COALESCE_WINDOW_SECONDS = 120.0
_ABSORB_MAX_AGE_SECONDS = 1800.0  # the update watcher's own deadline


def coalesce_window_seconds() -> float:
    """``updates.coalesce_window_seconds`` (default 120; 0 disables). Never raises."""
    try:
        from hermes_cli.config import load_config
        value = ((load_config() or {}).get("updates") or {}).get(
            "coalesce_window_seconds", DEFAULT_COALESCE_WINDOW_SECONDS)
    except Exception:  # health: allow BLE001 -- an unreadable config must not block /update; default applies
        logger.debug("updates.coalesce_window_seconds unreadable; using default", exc_info=True)
        return DEFAULT_COALESCE_WINDOW_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return DEFAULT_COALESCE_WINDOW_SECONDS
    return float(value)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _record_launch(home: Path, now: float) -> None:
    with contextlib.suppress(OSError):
        _write_json(home / LAST_LAUNCH_FILE, {"at": now})


def _last_launch(home: Path) -> float | None:
    data = _read_json(home / LAST_LAUNCH_FILE) or {}
    at = data.get("at")
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def _defer_locked(home: Path, pending: dict[str, Any], now: float, window: float) -> dict[str, Any]:
    deferred = _read_json(home / DEFERRED_FILE) or {}
    merged = int(deferred.get("merged") or 0) + 1
    last = _last_launch(home)
    due_at = max(now, (last if last is not None else now) + window)
    _write_json(home / DEFERRED_FILE, {
        "request": {k: v for k, v in pending.items() if k not in {"notification_version", "request_id"}},
        "merged": merged, "first_at": deferred.get("first_at", now), "updated_at": now,
    })
    return {"started": False, "pending": True, "coalesced": True, "merged": merged, "due_at": due_at}


def launch_deferred_update(*, home: Path, hermes_cmd: list[str], spawn: Callable[..., None],
                           window: float | None = None, now: float | None = None) -> dict[str, Any] | None:
    """Launch the deferred request once no install is in flight and the window has passed.

    None when nothing is due. The launch is an ordinary ``hermes update`` (``--branch`` for a
    branch request), so every updater guard still applies. A failed spawn keeps the request.
    """
    from gateway.update_notifications import locked_update_marker

    home = Path(home)
    if not (home / DEFERRED_FILE).exists():
        return None
    now = time.time() if now is None else now
    window = coalesce_window_seconds() if window is None else window
    with locked_update_marker(home):
        deferred = _read_json(home / DEFERRED_FILE)
        if not deferred or not isinstance(deferred.get("request"), dict):
            (home / DEFERRED_FILE).unlink(missing_ok=True)
            return None
        last = _last_launch(home)
        if last is not None and now < last + window:
            return None
        request = {**deferred["request"], "coalesced_requests": int(deferred.get("merged") or 0) + 1}
        branch = request.get("branch")
        result = _launch_locked(home, hermes_cmd, request, spawn,
                                update_args=["--branch", str(branch)] if branch else [])
        if result.pop("busy", None) is not None:
            return None
        _record_launch(home, now)
        (home / DEFERRED_FILE).unlink(missing_ok=True)
        return result


def launch_due_deferred(runner: Any, loop: Any) -> None:
    """Housekeeping chore: launch the one install a burst coalesced into, once it is due."""
    from gateway.run import _hermes_home, _resolve_update_hermes_bin
    from gateway.slash_commands import _spawn_detached_update
    if not (_hermes_home / DEFERRED_FILE).exists():
        return
    hermes_cmd = _resolve_update_hermes_bin(_hermes_home)
    if not hermes_cmd:
        return
    result = launch_deferred_update(home=_hermes_home, hermes_cmd=hermes_cmd, spawn=_spawn_detached_update)
    if result and result.get("started"):
        logger.info("Launched coalesced update request")
        loop.call_soon_threadsafe(runner._schedule_update_notification_watch)


def _deferred_is_fresh(home: Path, now: float) -> bool:
    """A queued request younger than the watcher's deadline; an older one was never launched
    (no hermes binary, a crashed housekeeping loop) and must not block an operator restart."""
    updated = (_read_json(home / DEFERRED_FILE) or {}).get("updated_at")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated):
        return False
    return now - float(updated) <= _ABSORB_MAX_AGE_SECONDS


def restart_absorbed_by_update(home: Path, *, now: float | None = None) -> str | None:
    """Why a plain restart is redundant now: an update in flight or queued restarts anyway."""
    from gateway.update_notifications import _timestamp, final_outcome, read_pending

    home = Path(home)
    now = time.time() if now is None else now
    if (home / DEFERRED_FILE).exists() and _deferred_is_fresh(home, now):
        return "an update is queued and will restart the gateway"
    current = read_pending(home)
    if current is None:
        return None
    try:
        age = now - _timestamp(str(current[1].get("timestamp")))
    except (TypeError, ValueError):
        return None
    # A marker older than the watcher's deadline belongs to an updater that died; never let it
    # block an operator restart.
    if age > _ABSORB_MAX_AGE_SECONDS:
        return None
    if current[0].name == ".update_pending.claimed.json" or final_outcome(home, current[1]) is None:
        return "an update is in progress and will restart the gateway"
    return None


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


def agent_update_handoff(result: dict[str, Any]) -> str:
    """The agent's turn-ending instruction; a merged request learns who gets the outcome."""
    return _UPDATE_COALESCED_HANDOFF if result.get("coalesced") else _UPDATE_HANDOFF


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
        profile_home = None
        if "profile" in payload:
            profile = payload["profile"]
            served = getattr(runner, "_served_profile_homes", None) or {}
            if not isinstance(profile, str) or profile not in served:
                return {"accepted": False, "error": "profile is not served by this gateway"}
            profile_home = served[profile]
        from gateway.run import GatewayRunner, _profile_runtime_scope
        try:
            # Socket executor threads carry the owner's scope, not the requesting turn's.
            scope = (_profile_runtime_scope(profile_home, hydrate_secrets=False)
                     if profile_home is not None else contextlib.nullcontext())
            with scope:
                db = runner._session_db._db
                resolved = _route_from_session_lineage(db, session_id)
        except Exception:
            resolved = None
        if resolved is None:
            return {"accepted": False, "error": "no deliverable messaging session route"}
        parent_session_id, route = resolved
        from gateway.config import Platform
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
        if profile_home is not None:
            pending["profile"] = payload["profile"]
        try:
            result = launch_native_update(home=home, hermes_cmd=hermes_cmd, pending=pending, spawn=spawn,
                                          coalesce_window=coalesce_window_seconds())
        except Exception:
            return {"accepted": False, "error": "native updater could not start"}
        if result["started"]:
            # Socket handlers run in an executor; the watcher creates asyncio tasks.
            main_loop.call_soon_threadsafe(runner._schedule_update_notification_watch)
        return {"accepted": True, **result, "handoff": agent_update_handoff(result)}

    return _handler
