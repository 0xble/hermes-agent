"""Cross-process primary-model cooldown state scoped to one Hermes home.

The in-memory fallback deadline is useful for a single agent, but gateway agent eviction,
cron workers, delegated children, and restarts create new objects and processes.  This module
keeps only the small provider-route breaker state on disk; callers still own model switching
and notice delivery.

Invariants:
- ``active`` and ``expired`` are shared lease states; ``probing`` has one owner and token.
- The state writer may arm or re-arm an outage and claim the sole probe.
- The probe owner may settle success/failure once, or release before a request leaves the primary.
- A successful primary response settles success in ``complete_primary_recovery``.
- Fallback activation and terminal failure settle failed at their primary exit point.
- Interrupt and cancellation paths release the probe without changing backoff.
- Classification and same-primary recovery never settle or release a probe lease.
"""
from __future__ import annotations

from agent.notice_style import ContextNotice

import contextlib
import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from dataclasses import dataclass
from enum import Enum
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
# A fresh gateway/cron worker may need several seconds to import and start.  Keep the
# first no-hint window at the documented one-minute floor so durable adoption does
# not race process startup into an immediate probe.
_FIRST_BACKOFF_SECONDS = 60
_PROBE_LEASE_SECONDS = 120
_RECOVERY_TOMBSTONE_SECONDS = 5 * 60
_RECOVERY_TOMBSTONE_GRACE_SECONDS = 2


class ProbeClaimKind(str, Enum):
    """The complete result space for an attempted half-open probe claim."""

    CLAIMED = "claimed"
    OWNED_ELSEWHERE = "owned_elsewhere"
    STILL_COOLING = "still_cooling"
    REPLACED = "replaced"
    CLEARED = "cleared"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProbeClaimOutcome:
    """Typed probe claim result; ``record`` is a snapshot for replacement/adoption."""

    kind: ProbeClaimKind
    token: str | None = None
    record: dict[str, Any] | None = None

    def __bool__(self) -> bool:
        return self.kind == ProbeClaimKind.CLAIMED


def _claim_outcome(kind: ProbeClaimKind, *, token: str | None = None,
                   record: dict[str, Any] | None = None) -> ProbeClaimOutcome:
    return ProbeClaimOutcome(kind=kind, token=token, record=dict(record) if isinstance(record, dict) else None)


def _probe_token_value(value: Any) -> str:
    """Extract the lease token without turning a typed no-token outcome into a token."""
    if isinstance(value, ProbeClaimOutcome):
        return value.token or ""
    return str(value or "")


def _state_path() -> Path:
    return get_hermes_home() / "state" / "model_cooldowns.json"


def _lock_path() -> Path:
    return _state_path().with_name(".model_cooldowns.lock")


_locked_provider_owners: ContextVar[dict[str, str | None] | None] = ContextVar(
    "cooldown_provider_owners", default=None,
)


def _provider_owners() -> dict[str, str | None]:
    """Cache identity resolution by profile and config revision, never by launch environment."""
    path = get_hermes_home() / "config.yaml"
    try:
        stat = path.stat()
        revision = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    except OSError:
        revision = None
    return _load_provider_owners(str(path), revision)


@lru_cache(maxsize=32)
def _load_provider_owners(path: str, revision: tuple | None) -> dict[str, str | None]:
    """Load once per revision; *path* keys the cache across multiplexed profiles."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.providers import custom_provider_slug
        config = load_config_readonly() or {}
    except (ImportError, OSError, TypeError, ValueError, KeyError):
        return {}
    matches: dict[str, list[tuple[str, str]]] = {}
    providers = config.get("providers") if isinstance(config, dict) else None
    if isinstance(providers, dict):
        for key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            url = entry.get("api") or entry.get("url") or entry.get("base_url")
            matches.setdefault(normalize_route_base_url(url), []).append((str(entry.get("name") or key), str(key)))
    legacy = config.get("custom_providers") if isinstance(config, dict) else None
    if isinstance(legacy, list):
        for entry in legacy:
            if not isinstance(entry, dict):
                continue
            url = entry.get("api") or entry.get("url") or entry.get("base_url")
            name = str(entry.get("name") or "").strip()
            if name:
                matches.setdefault(normalize_route_base_url(url), []).append((name, str(entry.get("provider_key") or "")))
    return {
        url: custom_provider_slug(*owners[0]).lower() if len(owners) == 1 else None
        for url, owners in matches.items()
    }


def _custom_provider_for_url(base_url: str) -> str | None:
    """Resolve aliases from one cached snapshot throughout a locked transaction."""
    owners = _locked_provider_owners.get()
    return (_provider_owners() if owners is None else owners).get(base_url)


def canonical_route(route: tuple[Any, Any, Any]) -> tuple[str, str, str]:
    """Normalize a route, resolving bare ``custom`` only when its owner is unambiguous."""
    provider, base_url, model = route
    provider = str(provider or "").strip().lower()
    base_url = normalize_route_base_url(base_url)
    model = str(model or "").strip()
    if provider == "custom":
        owner = _custom_provider_for_url(base_url)
        if owner:
            provider = owner
    return provider, base_url, model


def route_key(*, provider: Any, base_url: Any, model: Any) -> str:
    """Stable JSON key for a primary provider route."""
    provider, base_url, model = canonical_route((provider, base_url, model))
    return json.dumps(
        [provider, base_url, model],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def route_from_record(record: dict[str, Any]) -> tuple[str, str, str]:
    return canonical_route((
        str(record.get("provider") or "").strip().lower(),
        normalize_route_base_url(record.get("base_url")),
        str(record.get("model") or "").strip(),
    ))


def route_from_agent(agent: Any) -> tuple[str, str, str]:
    """The agent's configured primary route.

    Once the primary snapshot exists it is the only source: the live attributes may already
    describe a fallback, and mixing them in would key the record by the fallback's model.
    """
    runtime = getattr(agent, "_primary_runtime", None)
    if isinstance(runtime, dict) and runtime:
        return canonical_route((
            str(runtime.get("provider") or "").strip().lower(),
            normalize_route_base_url(runtime.get("base_url") or ""),
            str(runtime.get("model") or "").strip(),
        ))
    return canonical_route((
        str(getattr(agent, "provider", "") or "").strip().lower(),
        normalize_route_base_url(getattr(agent, "base_url", "") or ""),
        str(getattr(agent, "model", "") or "").strip(),
    ))


def live_route_from_agent(agent: Any) -> tuple[str, str, str]:
    """The route the agent is sending requests to right now (not its configured primary)."""
    return canonical_route((
        str(getattr(agent, "provider", "") or "").strip().lower(),
        normalize_route_base_url(getattr(agent, "base_url", "") or ""),
        str(getattr(agent, "model", "") or "").strip(),
    ))


def _route_fields(route: tuple[str, str, str]) -> dict[str, str]:
    provider, base_url, model = route
    return {"provider": provider, "base_url": base_url, "model": model}


@contextmanager
def _locked_state(*, quarantine_unreadable: bool = False) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Hold an exclusive lock while reading and atomically rewriting the state file."""
    # Config parsing and provider scans must never hold the cross-process state lock.
    owners = _provider_owners()
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
            backup = None
            try:
                try:
                    raw = json.loads(state_path.read_text(encoding="utf-8-sig"))
                except FileNotFoundError:
                    raw = {"routes": {}}
                except (ValueError, TypeError) as exc:
                    raise OSError(f"Malformed primary cooldown state at {state_path}") from exc
                if not isinstance(raw, dict) or not isinstance(raw.get("routes"), dict):
                    raise OSError(f"Malformed primary cooldown state at {state_path}")
            except OSError:
                if not quarantine_unreadable:
                    raise
                # Only explicit operator clear-all may recover, under the same writer lock.
                backup = state_path.with_name(f"{state_path.name}.corrupt-{time.time_ns()}")
                state_path.replace(backup)
                logger.warning("Quarantined unreadable primary cooldown state at %s", backup)
                raw = {"routes": {}}
            state = {"version": _STATE_VERSION, "routes": raw["routes"]}
            if backup is not None:
                state["quarantined"] = str(backup)
            scope = _locked_provider_owners.set(owners)
            try:
                yield state_path, state
            finally:
                _locked_provider_owners.reset(scope)
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
    tombstone_until = _finite_float(entry.get("tombstone_until"))
    if tombstone_until is not None:
        return tombstone_until <= now
    reset_at = _plausible_reset(entry.get("reset_at", 0), now)
    recorded_at = _finite_float(entry.get("recorded_at", reset_at)) if reset_at is not None else None
    if reset_at is None or recorded_at is None:
        return True
    probe_until = _finite_float(entry.get("probe_until"))
    if entry.get("probe_owner") and probe_until is not None and probe_until > now:
        return False  # A valid long-request lease outlasts the expired-record grace.
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


def _merge_duplicate_routes(state: dict[str, Any]) -> bool:
    """Collapse legacy route aliases while the state lock is held."""
    merged: dict[str, dict[str, Any]] = {}
    changed = False
    for raw_key, raw_entry in list(state["routes"].items()):
        if not isinstance(raw_entry, dict):
            continue
        route = route_from_record(raw_entry)
        key = route_key(provider=route[0], base_url=route[1], model=route[2])
        entry = dict(raw_entry)
        fields = _route_fields(route)
        if any(entry.get(k) != v for k, v in fields.items()) or raw_key != key:
            changed = True
        entry.update(fields)
        current = merged.get(key)
        if current is None:
            merged[key] = entry
            continue
        changed = True
        current_reset = _finite_float(current.get("reset_at")) or 0.0
        entry_reset = _finite_float(entry.get("reset_at")) or 0.0
        winner = entry if entry_reset > current_reset else current
        other = current if winner is entry else entry
        winner["backoff_count"] = max(int(winner.get("backoff_count", 0) or 0), int(other.get("backoff_count", 0) or 0))
        outage_ids = set(winner.get("merged_outage_ids") or ())
        outage_ids.update(other.get("merged_outage_ids") or ())
        for outage_id in (winner.get("outage_id"), other.get("outage_id")):
            if outage_id:
                outage_ids.add(str(outage_id))
        outage_ids.discard(str(winner.get("outage_id") or ""))
        if outage_ids:
            winner["merged_outage_ids"] = sorted(outage_ids)
        winner["notice_claimed"] = bool(winner.get("notice_claimed")) or bool(other.get("notice_claimed"))
        if not winner.get("notice_fallback") and other.get("notice_fallback"):
            winner["notice_fallback"] = other["notice_fallback"]
        merged[key] = winner
    if set(state["routes"]) != set(merged) or any(state["routes"].get(k) != v for k, v in merged.items()):
        state["routes"] = merged
        changed = True
    return changed


def _active(entry: Any, now: float | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    now = time.time() if now is None else now
    probe_until = _finite_float(entry.get("probe_until"))
    if probe_until is not None and probe_until > now:
        return True
    reset_at = _plausible_reset(entry.get("reset_at", 0), now)
    return reset_at is not None and reset_at > now and entry.get("state") != "tombstone"


def read_cooldown(route: tuple[str, str, str]) -> dict[str, Any] | None:
    """Read a route record like :func:`get_cooldown`, but let ``OSError`` propagate.

    Callers that treat a missing record as "the outage was cleared" must distinguish that
    from an unreadable state directory.
    """
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    with _locked_state() as (path, state):
        changed = _merge_duplicate_routes(state)
        changed = _prune_stale(state, time.time()) or changed
        if changed:
            _write_state(path, state)
        entry = state["routes"].get(key)
        if isinstance(entry, dict) and entry.get("state") == "tombstone":
            return None
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
    backoff_count: int | None = None, request_id: str | None = None,
    request_started_at: Any = None, short_window: bool = False,
) -> dict[str, Any] | None:
    """Create or re-arm a route outage and return its durable record."""
    now = time.time()
    # All callers may receive provider hints in epoch seconds, epoch milliseconds, or ISO-8601
    # form. Normalize at the durable boundary so a new caller cannot silently fall back to 60 s.
    from agent.fallback_cooldown import _provider_reset_epoch
    normalized_reset = _provider_reset_epoch(reset_at) if reset_at is not None else None
    requested_reset = _plausible_reset(normalized_reset, now) if normalized_reset is not None else None
    requested_reset = requested_reset or 0.0
    if requested_reset <= now:
        requested_reset = 0.0
    route = canonical_route(route)
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            changed = _merge_duplicate_routes(state)
            changed = _prune_stale(state, now) or changed
            old = state["routes"].get(key)
            if isinstance(old, dict) and old.get("state") == "tombstone":
                tombstone_until = _finite_float(old.get("tombstone_until")) or 0.0
                recovered_at = _finite_float(old.get("recorded_at"))
                started_at = _finite_float(request_started_at)
                late_request = started_at is not None and recovered_at is not None and started_at <= recovered_at
                within_grace = bool(request_id) and started_at is None and recovered_at is not None and now - recovered_at < _RECOVERY_TOMBSTONE_GRACE_SECONDS
                if tombstone_until > now and (late_request or within_grace):
                    if changed:
                        _write_state(path, state)
                    return None
            # Only a successful primary response clears the record, so an active or recently
            # expired entry is the same outage: escalate shared backoff, keep notice state. A
            # stale entry was pruned above, so a 429 long after expiry starts a new outage.
            same_outage = isinstance(old, dict) and old.get("state") != "tombstone"
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
            prior_ids = set(old.get("failure_request_ids") or ()) if same_outage else set()
            distinct_request = bool(request_id and request_id not in prior_ids)
            distinct_escalation = (
                distinct_request and isinstance(old, dict)
                and old_reset - float(old.get("recorded_at", 0) or 0) <= 20
            )
            if request_id:
                prior_ids.add(str(request_id))
            if requested_reset:
                effective_reset = requested_reset
                source = "provider_reset"
                backoff_count = max(1, prior_count + (0 if still_active and not distinct_escalation else 1))
            elif still_active:
                backoff_count = max(1, prior_count + (1 if distinct_escalation else 0))
                floor = _FIRST_BACKOFF_SECONDS if short_window and backoff_count <= 1 else 60 * (2 ** (backoff_count - 1))
                effective_reset = now + min(floor, _MAX_BACKOFF_SECONDS)
                source = str(old.get("source") or "backoff")
            else:
                backoff_count = max(1, prior_count + 1)
                floor = _FIRST_BACKOFF_SECONDS if short_window and backoff_count <= 1 else 60 * (2 ** (backoff_count - 1))
                effective_reset = now + min(floor, _MAX_BACKOFF_SECONDS)
                source = "backoff"
            if old_reset > effective_reset and not requested_reset:
                effective_reset = old_reset
                source = str(old.get("source") or source)
            retained_reason = str(old.get("reason") or "") if same_outage and old_reset > effective_reset else ""
            entry = {
                **_route_fields(route),
                "reset_at": effective_reset,
                "reason": retained_reason or getattr(reason, "value", str(reason or "rate_limit")),
                "source": source,
                "backoff_count": backoff_count,
                "notice_claimed": bool(old.get("notice_claimed")) if same_outage else False,
                "outage_id": outage_id,
                "recorded_at": now,
            }
            if same_outage:
                for field in ("merged_outage_ids", "probe_owner", "probe_until", "state"):
                    if field in old:
                        entry[field] = old[field]
            if prior_ids:
                entry["failure_request_ids"] = sorted(prior_ids)[-16:]
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
    route = canonical_route(route)
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


def clear_if_current(route: tuple[str, str, str], outage_id: str | None, *, tombstone: bool = False) -> bool:
    """Clear only the outage this agent observed; return true for the recovery-notice owner."""
    route = canonical_route(route)
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            _merge_duplicate_routes(state)
            entry = state["routes"].get(key)
            if not isinstance(entry, dict):
                return False
            accepted_ids = {str(entry.get("outage_id") or ""), *(str(value) for value in entry.get("merged_outage_ids") or ())}
            if outage_id is not None and str(outage_id) not in accepted_ids:
                return False
            if tombstone:
                now = time.time()
                entry.pop("probe_owner", None)
                entry.pop("probe_until", None)
                entry["state"] = "tombstone"
                entry["reset_at"] = now
                entry["recorded_at"] = now
                entry["tombstone_until"] = now + _RECOVERY_TOMBSTONE_SECONDS
                state["routes"][key] = entry
            else:
                del state["routes"][key]
            _write_state(path, state)
            return True
    except OSError as exc:
        logger.debug("Could not clear primary cooldown: %s", exc)
        return False


def claim_probe(
    route: tuple[str, str, str], outage_id: str | None, *, lease_seconds: float = _PROBE_LEASE_SECONDS,
) -> ProbeClaimOutcome:
    """Atomically claim the single half-open probe, returning every observable state explicitly."""
    now = time.time()
    route = canonical_route(route)
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    token = uuid.uuid4().hex
    try:
        with _locked_state() as (path, state):
            changed = _merge_duplicate_routes(state)
            changed = _prune_stale(state, now) or changed
            entry = state["routes"].get(key)
            accepted_ids = {
                str(entry.get("outage_id") or ""),
                *(str(value) for value in entry.get("merged_outage_ids") or ()),
            } if isinstance(entry, dict) else set()
            if not isinstance(entry, dict):
                if changed:
                    _write_state(path, state)
                return _claim_outcome(ProbeClaimKind.CLEARED)
            if str(outage_id) not in accepted_ids:
                if changed:
                    _write_state(path, state)
                return _claim_outcome(ProbeClaimKind.REPLACED, record=entry)
            if entry.get("state") == "tombstone":
                if changed:
                    _write_state(path, state)
                return _claim_outcome(ProbeClaimKind.CLEARED)
            probe_until = _finite_float(entry.get("probe_until")) or 0.0
            if probe_until > now:
                if changed:
                    _write_state(path, state)
                return _claim_outcome(ProbeClaimKind.OWNED_ELSEWHERE, record=entry)
            reset_at = _plausible_reset(entry.get("reset_at", 0), now)
            if reset_at is not None and reset_at > now:
                if changed:
                    _write_state(path, state)
                return _claim_outcome(ProbeClaimKind.STILL_COOLING, record=entry)
            entry["probe_owner"] = token
            entry["probe_until"] = now + max(_PROBE_LEASE_SECONDS, _finite_float(lease_seconds) or 0.0)
            entry["state"] = "probing"
            state["routes"][key] = entry
            _write_state(path, state)
            logger.info("Primary cooldown probe claimed route=%s outage_id=%s token=%s pid=%s", key, outage_id, token, os.getpid())
            return _claim_outcome(ProbeClaimKind.CLAIMED, token=token, record=entry)
    except OSError as exc:
        logger.debug("Could not claim primary cooldown probe: %s", exc)
        return _claim_outcome(ProbeClaimKind.UNAVAILABLE)


def _settle_probe(
    route: tuple[str, str, str], outage_id: str, token: str, *, success: bool,
    reset_at: Any = None,
) -> bool:
    """Settle only the lease owner, clearing on success or doubling on failure.

    A provider reset observed by the probe must survive the re-arm: the exponential window is
    still the floor, but a later valid reset hint wins.
    """
    if isinstance(token, ProbeClaimOutcome):
        token = _probe_token_value(token)
    now = time.time()
    route = canonical_route(route)
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            _merge_duplicate_routes(state)
            entry = state["routes"].get(key)
            if (
                not isinstance(entry, dict)
                or str(outage_id) not in {
                    str(entry.get("outage_id") or ""),
                    *(str(value) for value in entry.get("merged_outage_ids") or ()),
                }
                or str(entry.get("probe_owner") or "") != str(token)
            ):
                return False
            if success:
                entry.pop("probe_owner", None)
                entry.pop("probe_until", None)
                entry["state"] = "tombstone"
                entry["reset_at"] = now
                entry["recorded_at"] = now
                entry["tombstone_until"] = now + _RECOVERY_TOMBSTONE_SECONDS
                state["routes"][key] = entry
                _write_state(path, state)
                logger.info("Primary cooldown probe settled route=%s outage_id=%s token=%s", key, outage_id, token)
                return True
            count = max(1, int(entry.get("backoff_count", 0) or 0)) + 1
            entry.pop("probe_owner", None)
            entry.pop("probe_until", None)
            entry["state"] = "active"
            entry["backoff_count"] = count
            backoff_reset = now + min(60 * (2 ** max(0, count - 1)), _MAX_BACKOFF_SECONDS)
            from agent.fallback_cooldown import _provider_reset_epoch
            provider_reset = _provider_reset_epoch(reset_at)
            if (provider_reset is not None
                    and provider_reset <= now + _MAX_PROVIDER_RESET_SECONDS
                    and provider_reset > backoff_reset):
                entry["reset_at"] = provider_reset
                entry["source"] = "provider_reset"
            else:
                entry["reset_at"] = backoff_reset
                entry["source"] = "backoff"
            entry["recorded_at"] = now
            state["routes"][key] = entry
            _write_state(path, state)
            logger.info("Primary cooldown probe failed route=%s outage_id=%s token=%s", key, outage_id, token)
            return True
    except OSError as exc:
        logger.debug("Could not settle primary cooldown probe: %s", exc)
        return False


def _release_probe(route: tuple[str, str, str], outage_id: str, token: str) -> bool:
    """Release a probe lease when restoration never reached a request."""
    if isinstance(token, ProbeClaimOutcome):
        token = _probe_token_value(token)
    route = canonical_route(route)
    key = route_key(provider=route[0], base_url=route[1], model=route[2])
    try:
        with _locked_state() as (path, state):
            _merge_duplicate_routes(state)
            entry = state["routes"].get(key)
            accepted_ids = {
                str(entry.get("outage_id") or ""),
                *(str(value) for value in entry.get("merged_outage_ids") or ()),
            } if isinstance(entry, dict) else set()
            if (
                not isinstance(entry, dict)
                or str(outage_id) not in accepted_ids
                or str(entry.get("probe_owner") or "") != str(token)
            ):
                return False
            entry.pop("probe_owner", None)
            entry.pop("probe_until", None)
            entry["state"] = "expired"
            state["routes"][key] = entry
            _write_state(path, state)
            logger.info("Primary cooldown probe released route=%s outage_id=%s token=%s", key, outage_id, token)
            return True
    except OSError as exc:
        logger.debug("Could not release primary cooldown probe: %s", exc)
        return False


_PROBE_OUTAGE_REASONS = frozenset({
    "rate_limit", "upstream_rate_limit", "overloaded", "server_error", "timeout",
})


def _fail_agent_probe(agent, reason=None, reset_at=None) -> bool:
    """Settle an in-flight probe after an outage-class provider error, exactly once."""
    reason_value = getattr(reason, "value", reason)
    if reason is not None and reason_value not in _PROBE_OUTAGE_REASONS:
        return settle_primary_probe_on_exit(agent, reason="release")
    return settle_primary_probe_on_exit(agent, reason=reason, reset_at=reset_at)


def settle_primary_probe_on_exit(agent, *, reason=None, reset_at=None) -> bool:
    """Settle a held primary probe exactly when the request leaves the primary route.

    Same-primary recovery must leave the token untouched.  Outage-class exits re-arm the
    shared breaker. Only explicit interrupt/cancel exits release the lease without changing backoff.
    """
    raw_token = getattr(agent, "_shared_primary_probe_token", "")
    token = _probe_token_value(raw_token)
    record = getattr(agent, "_shared_primary_cooldown_record", None)
    if not token or not isinstance(record, dict):
        agent._shared_primary_probe_token = None
        return False
    route = route_from_record(record)
    outage_id = str(record.get("outage_id") or "")
    if reason in {"success", "primary_success"}:
        settled = _settle_probe(route, outage_id, token, success=True)
    elif reason in {"interrupt", "cancel", "release"}:
        settled = _release_probe(route, outage_id, token)
    else:
        reason_value = getattr(reason, "value", reason)
        if reason is not None and reason_value not in _PROBE_OUTAGE_REASONS:
            settled = _release_probe(route, outage_id, token)
        else:
            settled = _settle_probe(route, outage_id, token, success=False, reset_at=reset_at)
    # The primary exit consumes the local lease even if clear/takeover or an I/O
    # failure prevented settlement. A stale token must never bypass future arming.
    agent._shared_primary_probe_token = None
    if settled:
        if reason not in {"success", "primary_success", "interrupt", "cancel", "release"}:
            agent._shared_primary_probe_failed_outage_id = outage_id
            current = get_cooldown(route)
            if isinstance(current, dict):
                agent._shared_primary_cooldown_record = current
                agent._fallback_episode_cooled = True
                remaining = max(0, math.ceil(float(current.get("reset_at", 0) or 0) - time.time()))
                agent._rate_limited_until = time.monotonic() + remaining
                agent._rate_limit_backoff_count = int(current.get("backoff_count", 0) or 0)
    return settled


def primary_probe_is_owned(agent) -> bool:
    """Keep a local token only while the durable record still owns an unexpired lease."""
    token = _probe_token_value(getattr(agent, "_shared_primary_probe_token", None))
    record = getattr(agent, "_shared_primary_cooldown_record", None)
    if token and isinstance(record, dict):
        try:
            current = read_cooldown(route_from_record(record))
        except OSError:
            current = None
        if (isinstance(current, dict) and current.get("probe_owner") == token
                and (_finite_float(current.get("probe_until")) or 0.0) > time.time()):
            return True
    agent._shared_primary_probe_token = None
    return False


def list_cooldowns(*, strict: bool = False) -> list[dict[str, Any]]:
    """Return active (still cooling) records.

    ``strict=True`` propagates unreadable-state errors for operator status; the default keeps
    runtime/listing callers compatible with the historical empty result.

    Malformed and stale records are pruned from the state file. A recently expired record is
    kept, but not returned, so the first successful primary response can still clear it and
    own the recovery notice.
    """
    try:
        with _locked_state() as (path, state):
            now = time.time()
            changed = _merge_duplicate_routes(state)
            changed = _prune_stale(state, now) or changed
            if changed:
                _write_state(path, state)
            return [dict(entry) for entry in state["routes"].values() if _active(entry, now)]
    except OSError as exc:
        logger.debug("Could not list primary cooldowns: %s", exc)
        if strict:
            raise
        return []


def _matches_target(entry: dict[str, Any], target: str) -> bool:
    """Exact match on ``provider/model`` (provider case-insensitive) or on the bare model."""
    route = route_from_record(entry)
    provider, _, model = route
    if target == model:
        return True
    if "/" in target:
        wanted_provider, wanted_model = target.split("/", 1)
        wanted = canonical_route((wanted_provider, entry.get("base_url"), wanted_model))
        return wanted[0] == provider and wanted[2] == model
    return False


def clear_cooldowns(target: str | None = None, *, all_routes: bool = False) -> list[dict[str, Any]]:
    """Clear cooldowns (``all_routes`` or exact *target*), preserving recovery tombstones.

    *target* is ``provider/model`` or a bare model name, compared exactly (no substrings). A
    bare model clears that model on every provider. Returns the removed records. Raises
    ``ValueError`` when neither or both selectors are given, and ``OSError`` when the state
    cannot be updated, so the CLI never reports a clear that did not happen. Explicit
    ``all_routes`` quarantines an unreadable file under the writer lock and returns its
    backup path as a ``quarantined`` result; targeted clears keep strict read behavior.
    """
    wanted = str(target or "").strip()
    if all_routes == bool(wanted):
        raise ValueError("pass exactly one of a target or all_routes=True")
    with _locked_state(quarantine_unreadable=all_routes) as (path, state):
        if state.get("quarantined"):
            return [{"quarantined": state["quarantined"]}]
        _merge_duplicate_routes(state)
        removed = []
        for key, entry in list(state["routes"].items()):
            if not isinstance(entry, dict) or entry.get("state") == "tombstone":
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
    """Clear a shared outage after primary success; announce real backend recovery once.

    Request wrappers call this after every successful response, so it must prove the response
    came from the cooled primary: the agent's LIVE route (not its ``_primary_runtime`` snapshot)
    must equal the primary record. A successful fallback
    response separately clears only that fallback route's own breaker. Silent alias retries
    consume their recovery without a notice and retire that provenance.
    """
    fallback_record = getattr(agent, "_shared_fallback_cooldown_record", None)
    if getattr(agent, "_fallback_activated", False) and isinstance(fallback_record, dict):
        route = route_from_record(fallback_record)
        if live_route_from_agent(agent) != route:
            return False
        outage_id = str(fallback_record.get("outage_id") or "")
        if not outage_id or not clear_if_current(route, outage_id, tombstone=True):
            return False
        agent._shared_fallback_cooldown_record = None
        return True
    record = getattr(agent, "_shared_primary_cooldown_record", None)
    if not isinstance(record, dict):
        return False
    if getattr(agent, "_fallback_activated", False):
        return False
    route = route_from_record(record)
    if live_route_from_agent(agent) != route:
        return False
    outage_id = str(record.get("outage_id") or "")
    probe_token = _probe_token_value(getattr(agent, "_shared_primary_probe_token", ""))
    if not outage_id:
        return False
    if probe_token:
        cleared = settle_primary_probe_on_exit(agent, reason="success")
        if not cleared:
            # The lease may have been released or replaced before this successful primary
            # response arrived. A success on the live primary still proves the current outage
            # recovered, but never clear a lease another process currently owns.
            current = get_cooldown(route)
            if isinstance(current, dict) and not current.get("probe_owner"):
                cleared = clear_if_current(route, outage_id, tombstone=True)
    else:
        cleared = clear_if_current(route, outage_id, tombstone=True)
    if not cleared:
        return False
    agent._shared_primary_cooldown_record = None
    agent._shared_primary_probe_token = None
    agent._shared_primary_probe_failed_outage_id = None
    agent._rate_limited_until = 0
    agent._fallback_episode_cooled = False
    silent_retry = bool(record.get("_silent_retry", getattr(agent, "_provider_fallback_silent_retry", False)))
    agent._provider_fallback_silent_retry = False
    with contextlib.suppress(Exception):
        if not silent_retry:
            agent._emit_diagnostic_status(
                ContextNotice(f"✅ Primary model restored: {record.get('model') or route[2]} via "
                f"{record.get('provider') or route[0]}; fallback is no longer active."))
    return True


__all__ = [
    "ProbeClaimKind", "ProbeClaimOutcome", "active_cooldown", "announced_fallback", "arm_cooldown",
    "canonical_route", "claim_outage_notice", "claim_probe", "clear_cooldowns", "clear_if_current",
    "complete_primary_recovery", "get_cooldown", "is_active", "list_cooldowns", "live_route_from_agent",
    "read_cooldown", "route_from_agent", "route_from_record", "route_key", "settle_primary_probe_on_exit",
]
