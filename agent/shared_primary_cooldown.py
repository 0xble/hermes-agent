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
import math
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
# An expired record stays as the same outage only while a recovery probe is plausibly pending.
# Busy profiles probe within seconds of expiry, so a record nobody re-armed within the larger of
# 10 minutes and its own window (capped at the 4 h backoff ceiling) describes an outage that
# already ended. A 429 after that is a new outage: fresh id, unclaimed notice, backoff from 60 s.
_STALE_GRACE_FLOOR_SECONDS = 600
# Furthest-out reset a reader or writer accepts. Weekly usage caps reset within 7 days;
# 31 days leaves room for monthly billing caps without letting a corrupt value pin a route.
_MAX_PROVIDER_RESET_SECONDS = 31 * 86_400


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
    """The agent's configured primary route.

    Once the primary snapshot exists it is the only source: the live attributes may already
    describe a fallback, and mixing them in would key the record by the fallback's model.
    """
    runtime = getattr(agent, "_primary_runtime", None)
    if isinstance(runtime, dict) and runtime:
        return (
            str(runtime.get("provider") or "").strip().lower(),
            normalize_route_base_url(runtime.get("base_url") or ""),
            str(runtime.get("model") or "").strip(),
        )
    return (
        str(getattr(agent, "provider", "") or "").strip().lower(),
        normalize_route_base_url(getattr(agent, "base_url", "") or ""),
        str(getattr(agent, "model", "") or "").strip(),
    )


def live_route_from_agent(agent: Any) -> tuple[str, str, str]:
    """The route the agent is sending requests to right now (not its configured primary)."""
    return (
        str(getattr(agent, "provider", "") or "").strip().lower(),
        normalize_route_base_url(getattr(agent, "base_url", "") or ""),
        str(getattr(agent, "model", "") or "").strip(),
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
                raw = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {}
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


def _finite_float(value: Any) -> float | None:
    """``float(value)`` when it is a finite number, else None (NaN, ±Infinity and junk are malformed)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _plausible_reset(value: Any, now: float) -> float | None:
    """A finite reset no further out than ``_MAX_PROVIDER_RESET_SECONDS``, else None (malformed).

    Provider resets can legitimately be days away (weekly usage caps), so the ceiling is
    generous; it only rejects values no real provider sends, which would otherwise pin the
    route and overflow timestamp formatting.
    """
    reset_at = _finite_float(value)
    if reset_at is None or reset_at > now + _MAX_PROVIDER_RESET_SECONDS:
        return None
    return reset_at


def _stale(entry: Any, now: float) -> bool:
    """True when a record is malformed, or expired past its grace and no longer the current outage."""
    if not isinstance(entry, dict):
        return True
    reset_at = _plausible_reset(entry.get("reset_at", 0), now)
    recorded_at = _finite_float(entry.get("recorded_at", reset_at)) if reset_at is not None else None
    if reset_at is None or recorded_at is None:
        return True
    window = min(max(0.0, reset_at - recorded_at), float(_MAX_BACKOFF_SECONDS))
    return now > reset_at + max(float(_STALE_GRACE_FLOOR_SECONDS), window)


def _prune_stale(state: dict[str, Any], now: float) -> bool:
    """Drop malformed and stale records in place; return whether anything was removed."""
    changed = False
    for key, entry in list(state["routes"].items()):
        if _stale(entry, now):
            del state["routes"][key]
            changed = True
    return changed


def _active(entry: Any, now: float | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    now = time.time() if now is None else now
    reset_at = _plausible_reset(entry.get("reset_at", 0), now)
    return reset_at is not None and reset_at > now


def read_cooldown(route: tuple[str, str, str]) -> dict[str, Any] | None:
    """Read a route record like :func:`get_cooldown`, but let ``OSError`` propagate.

    Callers that treat a missing record as "the outage was cleared" must distinguish that
    from an unreadable state directory.
    """
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    with _locked_state() as (path, state):
        if _prune_stale(state, time.time()):
            _write_state(path, state)
        entry = state["routes"].get(key)
        return dict(entry) if isinstance(entry, dict) else None


def get_cooldown(route: tuple[str, str, str]) -> dict[str, Any] | None:
    """Read a route record, including a recently expired record until recovery clears it.

    Stale records (see ``_stale``) are pruned and never returned.
    """
    try:
        return read_cooldown(route)
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
    requested_reset = (_plausible_reset(reset_at, now) or 0.0) if reset_at is not None else 0.0
    if requested_reset <= now:
        requested_reset = 0.0
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            _prune_stale(state, now)
            old = state["routes"].get(key)
            # Only a successful primary response clears the record, so an active or recently
            # expired entry is the same outage: escalate shared backoff, keep notice state. A
            # stale entry was pruned above, so a 429 long after expiry starts a new outage.
            same_outage = isinstance(old, dict)
            if same_outage:
                outage_id = str(old.get("outage_id") or f"{now:.6f}-{os.getpid()}-{threading.get_ident()}")
                prior_count = max(int(old.get("backoff_count", 0) or 0), int(backoff_count or 0))
            else:
                outage_id = f"{now:.6f}-{os.getpid()}-{threading.get_ident()}"
                prior_count = max(0, int(backoff_count or 0))
            # A 429 that lands while the record is still active comes from a request that was
            # already in flight before the outage was recorded, not from a fresh probe, so it
            # must neither escalate the backoff nor shorten the window everyone else honours.
            still_active = _active(old, now)
            old_reset = float(old.get("reset_at", 0) or 0) if still_active else 0.0
            if requested_reset:
                effective_reset = requested_reset
                source = "provider_reset"
                backoff_count = max(1, prior_count + (0 if still_active else 1))
            elif still_active:
                backoff_count = max(1, prior_count)
                effective_reset = now + min(60 * (2 ** (backoff_count - 1)), _MAX_BACKOFF_SECONDS)
                source = str(old.get("source") or "backoff")
            else:
                backoff_count = prior_count + 1
                effective_reset = now + min(60 * (2 ** max(0, backoff_count - 1)), _MAX_BACKOFF_SECONDS)
                source = "backoff"
            if old_reset > effective_reset and not requested_reset:
                effective_reset = old_reset
                source = str(old.get("source") or source)
            entry = {
                **_route_fields(route),
                "reset_at": effective_reset,
                "reason": getattr(reason, "value", str(reason or "rate_limit")),
                "source": source,
                "backoff_count": backoff_count,
                "notice_claimed": bool(old.get("notice_claimed")) if same_outage else False,
                "outage_id": outage_id,
                "recorded_at": now,
            }
            if same_outage and old.get("notice_fallback"):
                entry["notice_fallback"] = old["notice_fallback"]
            state["routes"][key] = entry
            _write_state(path, state)
            return dict(entry)
    except OSError as exc:
        logger.warning("Could not persist primary cooldown for %s/%s: %s", route[0], route[2], exc)
        return None


def claim_outage_notice(
    route: tuple[str, str, str], outage_id: str, *, fallback: tuple[str, str] | None = None,
) -> bool:
    """Atomically claim the sole user-facing outage notice for an outage.

    ``fallback`` is the (model, provider) the notice announces, so later switches can tell
    whether the user was already told about the model they are moving to.
    """
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            entry = state["routes"].get(key)
            if not isinstance(entry, dict) or str(entry.get("outage_id")) != str(outage_id):
                return False
            if entry.get("notice_claimed"):
                return False
            entry["notice_claimed"] = True
            if fallback is not None:
                entry["notice_fallback"] = [str(fallback[0]), str(fallback[1]).strip().lower()]
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
    """Return active (still cooling) records.

    Malformed and stale records are pruned from the state file. A recently expired record is
    kept, but not returned, so the first successful primary response can still clear it and
    own the recovery notice.
    """
    try:
        with _locked_state() as (path, state):
            now = time.time()
            if _prune_stale(state, now):
                _write_state(path, state)
            return [dict(entry) for entry in state["routes"].values() if _active(entry, now)]
    except OSError as exc:
        logger.debug("Could not list primary cooldowns: %s", exc)
        return []


def _matches_target(entry: dict[str, Any], target: str) -> bool:
    """Exact match on ``provider/model`` (provider case-insensitive) or on the bare model."""
    provider = str(entry.get("provider") or "").strip().lower()
    model = str(entry.get("model") or "").strip()
    if target == model:
        return True
    if "/" in target:
        wanted_provider, wanted_model = target.split("/", 1)
        return wanted_provider.strip().lower() == provider and wanted_model.strip() == model
    return False


def clear_cooldowns(target: str | None = None, *, all_routes: bool = False) -> list[dict[str, Any]]:
    """Clear every record (``all_routes``) or the records exactly matching *target*.

    *target* is ``provider/model`` or a bare model name, compared exactly (no substrings). A
    bare model clears that model on every provider. Returns the removed records. Raises
    ``ValueError`` when neither or both selectors are given, and ``OSError`` when the state
    cannot be updated, so the CLI never reports a clear that did not happen.
    """
    wanted = str(target or "").strip()
    if all_routes == bool(wanted):
        raise ValueError("pass exactly one of a target or all_routes=True")
    with _locked_state() as (path, state):
        removed = []
        for key, entry in list(state["routes"].items()):
            if not isinstance(entry, dict):
                continue
            if all_routes or _matches_target(entry, wanted):
                removed.append(dict(entry))
                del state["routes"][key]
        if removed:
            _write_state(path, state)
        return removed


def is_active(entry: dict[str, Any] | None) -> bool:
    return _active(entry)


def announced_fallback(route: tuple[str, str, str], outage_id: str) -> tuple[str, str] | None:
    """The (model, provider) already announced for this outage, or None when unannounced."""
    entry = get_cooldown(route)
    if not isinstance(entry, dict) or str(entry.get("outage_id")) != str(outage_id):
        return None
    if not entry.get("notice_claimed"):
        return None
    announced = entry.get("notice_fallback")
    if isinstance(announced, (list, tuple)) and len(announced) == 2:
        return str(announced[0]), str(announced[1]).strip().lower()
    return None


def complete_primary_recovery(agent) -> bool:
    """Clear a shared outage after a successful primary response and emit one recovery notice.

    Request wrappers call this after every successful response, so it must prove the response
    came from the cooled primary: the agent's LIVE route (not its ``_primary_runtime`` snapshot)
    must equal the record's route and no fallback may be active. A fallback reply never clears.
    """
    record = getattr(agent, "_shared_primary_cooldown_record", None)
    if not isinstance(record, dict):
        return False
    if getattr(agent, "_fallback_activated", False):
        return False
    route = route_from_record(record)
    if live_route_from_agent(agent) != route:
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
    "active_cooldown", "announced_fallback", "arm_cooldown", "claim_outage_notice", "clear_cooldowns",
    "clear_if_current", "complete_primary_recovery", "get_cooldown", "is_active", "list_cooldowns",
    "live_route_from_agent", "read_cooldown", "route_from_agent", "route_from_record", "route_key",
]
