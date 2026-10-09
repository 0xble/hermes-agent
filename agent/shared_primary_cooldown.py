"""Cross-process primary-model cooldown state scoped to one Hermes home.

The in-memory fallback deadline is useful for a single agent, but gateway agent eviction,
cron workers, delegated children, and restarts create new objects and processes.  This module
keeps only the small provider-route breaker state on disk; callers still own model switching
and notice delivery.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import get_hermes_home
from hermes_cli.route_identity import normalize_route_base_url
from utils import atomic_write_text

logger = logging.getLogger(__name__)

_STATE_VERSION = 1
_MAX_BACKOFF_SECONDS = 14_400


def _state_path() -> Path:
    return get_hermes_home() / "state" / "model_cooldowns.json"


def _lock_path() -> Path:
    return _state_path().with_name(".model_cooldowns.lock")


def route_key(*, provider: Any, base_url: Any, model: Any) -> str:
    """Stable JSON key for a primary provider route."""
    return json.dumps(
        [str(provider or "").strip().lower(), normalize_route_base_url(base_url), str(model or "").strip()],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def route_from_record(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("provider") or "").strip().lower(),
        normalize_route_base_url(record.get("base_url")),
        str(record.get("model") or "").strip(),
    )


def route_from_agent(agent: Any) -> tuple[str, str, str]:
    runtime = getattr(agent, "_primary_runtime", None) or {}
    return (
        str(runtime.get("provider") or getattr(agent, "provider", "") or "").strip().lower(),
        normalize_route_base_url(runtime.get("base_url") or getattr(agent, "base_url", "")),
        str(runtime.get("model") or getattr(agent, "model", "") or "").strip(),
    )


def _route_fields(route: tuple[str, str, str]) -> dict[str, str]:
    provider, base_url, model = route
    return {"provider": provider, "base_url": base_url, "model": model}


@contextmanager
def _locked_state() -> Iterator[tuple[Path, dict[str, Any]]]:
    """Hold an exclusive lock while reading and atomically rewriting the state file."""
    state_path = _state_path()
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover - Windows-only branch
            import msvcrt
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(" ")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            unlock = lambda: (lock_file.seek(0), msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1))
        try:
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            except (OSError, ValueError, TypeError):
                logger.warning("Ignoring unreadable primary cooldown state at %s", state_path)
                raw = {}
            state = raw if isinstance(raw, dict) else {}
            routes = state.get("routes")
            if not isinstance(routes, dict):
                routes = {}
            state = {"version": _STATE_VERSION, "routes": routes}
            yield state_path, state
        finally:
            unlock()


def _write_state(path: Path, state: dict[str, Any]) -> None:
    routes = state.get("routes") or {}
    if routes:
        atomic_write_text(
            path,
            json.dumps({"version": _STATE_VERSION, "routes": routes}, indent=2, sort_keys=True),
            mode=0o600,
            fsync_dir=True,
        )
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _active(entry: Any, now: float | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        return float(entry.get("reset_at", 0)) > (time.time() if now is None else now)
    except (TypeError, ValueError):
        return False


def get_cooldown(route: tuple[str, str, str]) -> dict[str, Any] | None:
    """Read a route record, including an expired record until recovery clears it."""
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (_path, state):
            entry = state["routes"].get(key)
            return dict(entry) if isinstance(entry, dict) else None
    except OSError as exc:
        logger.debug("Primary cooldown read failed: %s", exc)
        return None


def active_cooldown(route: tuple[str, str, str]) -> dict[str, Any] | None:
    entry = get_cooldown(route)
    return entry if _active(entry) else None


def arm_cooldown(
    route: tuple[str, str, str], *, reason: Any, reset_at: Any = None,
    backoff_count: int | None = None,
) -> dict[str, Any] | None:
    """Create or re-arm a route outage and return its durable record."""
    now = time.time()
    try:
        requested_reset = float(reset_at) if reset_at is not None else 0.0
    except (TypeError, ValueError):
        requested_reset = 0.0
    if requested_reset <= now:
        requested_reset = 0.0
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            old = state["routes"].get(key)
            old_active = _active(old, now)
            if old_active:
                outage_id = str(old.get("outage_id") or f"{now:.6f}-{os.getpid()}-{threading.get_ident()}")
                prior_count = int(old.get("backoff_count", 0) or 0)
            else:
                outage_id = f"{now:.6f}-{os.getpid()}-{threading.get_ident()}"
                prior_count = max(0, int(backoff_count or 0))
            if requested_reset:
                effective_reset = requested_reset
                source = "provider_reset"
                backoff_count = max(1, prior_count + (0 if old_active else 1))
            else:
                backoff_count = prior_count + 1
                effective_reset = now + min(60 * (2 ** max(0, backoff_count - 1)), _MAX_BACKOFF_SECONDS)
                source = "backoff"
            entry = {
                **_route_fields(route),
                "reset_at": effective_reset,
                "reason": getattr(reason, "value", str(reason or "rate_limit")),
                "source": source,
                "backoff_count": backoff_count,
                "notice_claimed": bool(old.get("notice_claimed")) if old_active else False,
                "outage_id": outage_id,
                "recorded_at": now,
            }
            state["routes"][key] = entry
            _write_state(path, state)
            return dict(entry)
    except OSError as exc:
        logger.warning("Could not persist primary cooldown for %s/%s: %s", route[0], route[2], exc)
        return None


def claim_outage_notice(route: tuple[str, str, str], outage_id: str) -> bool:
    """Atomically claim the sole user-facing outage notice for an outage."""
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            entry = state["routes"].get(key)
            if not isinstance(entry, dict) or str(entry.get("outage_id")) != str(outage_id):
                return False
            if entry.get("notice_claimed"):
                return False
            entry["notice_claimed"] = True
            state["routes"][key] = entry
            _write_state(path, state)
            return True
    except OSError as exc:
        logger.debug("Could not claim primary outage notice: %s", exc)
        return False


def clear_if_current(route: tuple[str, str, str], outage_id: str | None) -> bool:
    """Clear only the outage this agent observed; return true for the recovery-notice owner."""
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            entry = state["routes"].get(key)
            if not isinstance(entry, dict):
                return False
            if outage_id is not None and str(entry.get("outage_id")) != str(outage_id):
                return False
            del state["routes"][key]
            _write_state(path, state)
            return True
    except OSError as exc:
        logger.debug("Could not clear primary cooldown: %s", exc)
        return False


def list_cooldowns() -> list[dict[str, Any]]:
    """Return active records and remove expired records that have no recovery owner."""
    try:
        with _locked_state() as (path, state):
            now = time.time()
            active = []
            changed = False
            for key, entry in list(state["routes"].items()):
                if not isinstance(entry, dict):
                    del state["routes"][key]
                    changed = True
                elif _active(entry, now):
                    active.append(dict(entry))
                else:
                    # Expired records remain until a successful primary restore so the recovery
                    # owner can claim the notice. CLI status should not display stale outages.
                    continue
            if changed:
                _write_state(path, state)
            return active
    except OSError as exc:
        logger.debug("Could not list primary cooldowns: %s", exc)
        return []


def clear_cooldowns(model: str | None = None) -> int:
    """Clear all cooldowns, or routes whose model/provider contains *model*."""
    wanted = str(model or "").strip().lower()
    try:
        with _locked_state() as (path, state):
            removed = 0
            for key, entry in list(state["routes"].items()):
                if not isinstance(entry, dict):
                    continue
                haystack = f"{entry.get('provider', '')}/{entry.get('model', '')}".lower()
                if not wanted or wanted in haystack:
                    del state["routes"][key]
                    removed += 1
            if removed:
                _write_state(path, state)
            return removed
    except OSError as exc:
        logger.debug("Could not clear primary cooldowns: %s", exc)
        return 0


def is_active(entry: dict[str, Any] | None) -> bool:
    return _active(entry)


def complete_primary_recovery(agent) -> bool:
    """Clear a shared outage after a successful primary response and emit one recovery notice."""
    record = getattr(agent, "_shared_primary_cooldown_record", None)
    if not isinstance(record, dict):
        return False
    route = route_from_agent(agent)
    if route != route_from_record(record):
        return False
    outage_id = str(record.get("outage_id") or "")
    if not outage_id or not clear_if_current(route, outage_id):
        return False
    agent._shared_primary_cooldown_record = None
    with contextlib.suppress(Exception):
        agent._emit_diagnostic_status(
            f"✅ Primary model restored: {record.get('model') or route[2]} via "
            f"{record.get('provider') or route[0]}; fallback is no longer active."
        )
    return True
__all__ = [
    "active_cooldown", "arm_cooldown", "claim_outage_notice", "clear_cooldowns", "clear_if_current",
    "complete_primary_recovery", "get_cooldown", "is_active", "list_cooldowns", "route_from_agent", "route_from_record", "route_key",
]

