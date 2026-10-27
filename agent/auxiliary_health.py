"""Typed, profile-scoped health for auxiliary provider routes.

Auxiliary route health is deliberately separate from the main-agent primary cooldown
policy. It shares only route identity and a small file-backed persistence contract so
auxiliary callers in the same Hermes home can skip a known-bad route after a restart or
when another worker recorded the failure.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional

from agent.incognito import _lock as _privacy_lock
from agent.incognito import is_incognito, private_state
from hermes_cli.route_identity import normalize_route_base_url
from hermes_constants import get_hermes_home, hermes_home_key
from utils import atomic_write_text

logger = logging.getLogger(__name__)


class AuxiliaryHealthLane(str, Enum):
    """Failure scope that can be shared across auxiliary callers."""

    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    ENDPOINT_OUTAGE = "endpoint_outage"
    MODEL = "model"


@dataclass(frozen=True)
class AuxiliaryRouteIdentity:
    """Non-secret identity used to share auxiliary route health."""

    home: str
    provider: str
    endpoint: str = ""
    model: str = ""

    def as_key(self) -> tuple[str, str, str, str]:
        return self.home, self.provider, self.endpoint, self.model


@dataclass(frozen=True)
class AuxiliaryHealthRecord:
    """Persisted route quarantine state."""

    expires_at: float
    lane: AuxiliaryHealthLane
    reason: str
    epoch: int


_STATE_VERSION = 1
_STATE_NAME = "auxiliary_route_health.json"
_LOCK_NAME = ".auxiliary_route_health.lock"
_MAX_RECORD_SECONDS = 31 * 86_400
_STALE_GRACE_SECONDS = 600.0
_PROBE_NONCE = uuid.uuid4().hex
_private_lock = threading.RLock()


def _probe_owner() -> str:
    """Return a process/thread-specific owner token for recovery probes."""
    return f"{os.getpid()}:{threading.get_ident()}:{_PROBE_NONCE}"


def _state_path() -> Path:
    return get_hermes_home() / "state" / _STATE_NAME


def _lock_path() -> Path:
    return _state_path().with_name(_LOCK_NAME)


def _unhealthy_cache_key(
    provider: str, base_url: Optional[str] = None, model: Optional[str] = None,
) -> tuple[str, str, str, str]:
    """Return a profile/endpoint/model-scoped, non-secret route key."""
    from agent.auxiliary_client import _normalize_chain_label
    label = _normalize_chain_label(provider)
    endpoint = normalize_route_base_url(_custom_health_base_url(provider, base_url))
    return AuxiliaryRouteIdentity(
        home=hermes_home_key(), provider=label, endpoint=endpoint, model=str(model or "").strip(),
    ).as_key()


def _custom_health_base_url(provider: str, explicit_base_url: Optional[str] = None) -> str:
    """Return the concrete custom endpoint used to scope health and failed-route checks.

    Only custom routes are endpoint-scoped. Known providers stay provider-wide so the
    discovery, api-key and fallback walks, which check without a URL, see the same record
    that a failure on the concrete client URL wrote.
    """
    explicit = str(explicit_base_url or "").strip()
    # Keep named custom identity until its endpoint has been resolved.
    label = str(provider or "").strip().lower()
    if label in {"custom", "local/custom"}:
        if explicit:
            return explicit
        with contextlib.suppress(Exception):
            from agent.auxiliary_client import _current_custom_base_url
            return str(_current_custom_base_url() or "").strip()
        return ""
    if label.startswith("custom:") and explicit:
        return explicit
    with contextlib.suppress(ImportError):
        from hermes_cli.runtime_provider import _get_named_custom_provider, _resolves_to_custom
        if _resolves_to_custom(label):
            with contextlib.suppress(Exception):
                from agent.auxiliary_client import _current_custom_base_url
                return explicit or str(_current_custom_base_url() or "").strip()
            return explicit
        entry = _get_named_custom_provider(provider)
        if entry:
            return explicit or str(entry.get("base_url") or "").strip()
    return ""


def _failure_lane(reason: Optional[str]) -> AuxiliaryHealthLane:
    lowered = str(reason or "").strip().lower()
    if any(marker in lowered for marker in ("auth", "credential", "token", "jwt")):
        return AuxiliaryHealthLane.AUTH
    if any(marker in lowered for marker in ("payment", "credit", "quota", "billing", "usage limit")):
        return AuxiliaryHealthLane.QUOTA
    if any(marker in lowered for marker in ("rate limit", "rate_limit", "429", "retry after")):
        return AuxiliaryHealthLane.RATE_LIMIT
    if any(marker in lowered for marker in ("model", "entitlement", "unsupported")):
        return AuxiliaryHealthLane.MODEL
    return AuxiliaryHealthLane.ENDPOINT_OUTAGE


def _shareable_failure_reason(reason: Optional[str]) -> bool:
    """Task/parser failures must not poison a route shared by other callers."""
    lowered = str(reason or "").strip().lower()
    return "invalid provider response" not in lowered and "malformed" not in lowered


def _raw_epoch(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _raw_expiry(value: Any, now: float) -> Optional[float]:
    try:
        expiry = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(expiry) or expiry > now + _MAX_RECORD_SECONDS:
        return None
    return expiry


def _finite_expiry(value: Any, now: float) -> Optional[float]:
    expiry = _raw_expiry(value, now)
    return expiry if expiry is not None and expiry > now else None


def _record_key(key: tuple[str, str, str, str]) -> str:
    return json.dumps(list(key), ensure_ascii=True, separators=(",", ":"))


def _parse_key(value: Any) -> Optional[tuple[str, str, str, str]]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or len(parsed) != 4 or not all(isinstance(part, str) for part in parsed):
        return None
    return tuple(parsed)  # type: ignore[return-value]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"version": _STATE_VERSION, "routes": {}}
    except (OSError, ValueError, TypeError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
        with contextlib.suppress(OSError):
            path.replace(backup)
        logger.warning("Quarantined unreadable auxiliary route-health state at %s: %s", backup, exc)
        return {"version": _STATE_VERSION, "routes": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("routes"), dict):
        return {"version": _STATE_VERSION, "routes": {}}
    return {"version": _STATE_VERSION, "routes": dict(raw["routes"])}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    if is_incognito():
        return
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


@contextmanager
def _locked_state() -> Iterator[tuple[Path, dict[str, Any]]]:
    # Admission and the complete short file transaction share the transition
    # lock, as state.db writes do. No provider request runs inside this boundary.
    with _privacy_lock:
        private = private_state("auxiliary_route_health")
        if private is not None:
            # The shared read path also creates locks, repairs and prunes. Keep
            # the entire transaction in RAM, not just its final write.
            with _private_lock:
                private.setdefault("version", _STATE_VERSION)
                private.setdefault("routes", {})
                yield _state_path(), private
            return
        path = _state_path()
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
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                unlock = lambda: (lock_file.seek(0), msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1))  # type: ignore[attr-defined]
            try:
                yield path, _load_state(path)
            finally:
                unlock()


def _prune(state: dict[str, Any], now: float) -> bool:
    changed = False
    for raw_key, raw_record in list(state["routes"].items()):
        key = _parse_key(raw_key)
        if key is None or not isinstance(raw_record, dict):
            del state["routes"][raw_key]
            changed = True
            continue
        expiry = _raw_expiry(raw_record.get("expires_at"), now)
        try:
            recorded = float(raw_record.get("recorded_at", now))
        except (TypeError, ValueError):
            recorded = now
        if expiry is None or now > expiry + max(_STALE_GRACE_SECONDS, min(_MAX_RECORD_SECONDS, expiry - recorded)):
            del state["routes"][raw_key]
            changed = True
    return changed


def read_health(identity: AuxiliaryRouteIdentity) -> Optional[AuxiliaryHealthRecord]:
    now = time.time()
    with _locked_state() as (path, state):
        changed = _prune(state, now)
        raw = state["routes"].get(_record_key(identity.as_key()))
        if changed:
            _write_state(path, state)
        if not isinstance(raw, dict):
            return None
        expiry = _finite_expiry(raw.get("expires_at"), now)
        if expiry is None:
            return None
        try:
            lane = AuxiliaryHealthLane(str(raw.get("lane")))
            epoch = _raw_epoch(raw.get("epoch", 1), default=1)
        except (ValueError, TypeError):
            return None
        return AuxiliaryHealthRecord(expiry, lane, str(raw.get("reason") or ""), epoch)


def mark_unhealthy(
    identity: AuxiliaryRouteIdentity, *, ttl: float, reason: str,
) -> Optional[AuxiliaryHealthRecord]:
    if not _shareable_failure_reason(reason):
        return None
    now = time.time()
    expiry = now + max(0.0, float(ttl))
    lane = _failure_lane(reason)
    with _locked_state() as (path, state):
        _prune(state, now)
        raw_key = _record_key(identity.as_key())
        previous = state["routes"].get(raw_key)
        previous_expiry = _finite_expiry(previous.get("expires_at"), now) if isinstance(previous, dict) else None
        previous_lane = str(previous.get("lane")) if isinstance(previous, dict) else ""
        previous_epoch = _raw_epoch(previous.get("epoch", 0)) if isinstance(previous, dict) else 0
        epoch = previous_epoch if previous_expiry and previous_lane == lane else previous_epoch + 1
        expiry = max(expiry, previous_expiry or 0.0)
        record = {
            "expires_at": expiry, "lane": lane.value, "reason": reason,
            "epoch": epoch, "notice_epoch": previous.get("notice_epoch", 0) if isinstance(previous, dict) else 0,
            "probe_until": 0.0, "recorded_at": now,
        }
        state["routes"][raw_key] = record
        _write_state(path, state)
    return AuxiliaryHealthRecord(expiry, lane, reason, epoch)


# Long enough to cover the compression timeout floor, so one slow probe is not joined by a
# second concurrent probe before it can report.
_PROBE_CLAIM_SECONDS = 300.0


def claim_probe(identity: AuxiliaryRouteIdentity, *, ttl: float = _PROBE_CLAIM_SECONDS) -> Optional[bool]:
    """Claim the sole recovery probe for an expired, recently-held route.

    Returns None when no route record exists, True for the caller that owns the probe,
    and False when another caller already owns it.
    """
    now = time.time()
    with _locked_state() as (path, state):
        _prune(state, now)
        raw_key = _record_key(identity.as_key())
        record = state["routes"].get(raw_key)
        if not isinstance(record, dict):
            return None
        expiry = _raw_expiry(record.get("expires_at"), now)
        if expiry is None:
            return None
        if expiry > now:
            return False
        try:
            probe_until = float(record.get("probe_until", 0.0))
        except (TypeError, ValueError):
            probe_until = 0.0
        if probe_until > now:
            return False
        record["probe_until"] = now + max(0.1, float(ttl))
        record["probe_owner"] = _probe_owner()
        state["routes"][raw_key] = record
        _write_state(path, state)
        return True


def claim_notice(identity: AuxiliaryRouteIdentity, epoch: Optional[int] = None) -> bool:
    """Atomically claim one generic notice for a route health epoch.

    When *epoch* is omitted, claim the current persisted epoch even after expiry. The
    recovery probe keeps that record around long enough for concurrent callers to
    deduplicate their skip notices before the route is cleared.
    """
    with _locked_state() as (path, state):
        raw_key = _record_key(identity.as_key())
        record = state["routes"].get(raw_key)
        if not isinstance(record, dict):
            return False
        current_epoch = _raw_epoch(record.get("epoch", 0))
        if epoch is not None and current_epoch != _raw_epoch(epoch, default=-1):
            return False
        if _raw_epoch(record.get("notice_epoch", 0)) == current_epoch:
            return False
        record["notice_epoch"] = current_epoch
        state["routes"][raw_key] = record
        _write_state(path, state)
        return True


def clear_health_after_probe(identity: AuxiliaryRouteIdentity) -> bool:
    """Clear an expired record only for the caller that owns its recovery probe.

    Ownership, not the claim window, decides: a probe that outlives its claim still clears
    on success unless another caller has since claimed or the route was marked again.
    """
    now = time.time()
    with _locked_state() as (path, state):
        _prune(state, now)
        raw_key = _record_key(identity.as_key())
        record = state["routes"].get(raw_key)
        if not isinstance(record, dict):
            return False
        expiry = _raw_expiry(record.get("expires_at"), now)
        if expiry is None or expiry > now:
            return False
        if record.get("probe_owner") != _probe_owner():
            return False
        del state["routes"][raw_key]
        _write_state(path, state)
        return True


def clear_health(identity: AuxiliaryRouteIdentity) -> None:
    with _locked_state() as (path, state):
        if state["routes"].pop(_record_key(identity.as_key()), None) is not None:
            _write_state(path, state)


def reset_health_state() -> None:
    with _locked_state() as (path, state):
        state["routes"].clear()
        _write_state(path, state)


def fallback_candidate_unavailable_reason(exc: Exception) -> Optional[str]:
    """Why a fallback candidate cannot serve this walk, or None."""
    from agent.auxiliary_client import _FALLBACK_REASONS
    return next(
        (label for predicate, label in _FALLBACK_REASONS if label != "auth error" and predicate(exc)),
        None,
    )


_TRANSIENT_CANDIDATE_QUARANTINE_SECONDS = 60.0
_CANDIDATE_QUARANTINE_TTL: dict[str, Optional[float]] = {
    "rate limit": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "connection error": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "invalid provider response": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
}


def fallback_candidate_quarantine_ttl(reason: Optional[str]) -> Optional[float]:
    """Seconds to hide a fallback candidate for ``reason``; None means the long default TTL."""
    return _CANDIDATE_QUARANTINE_TTL.get(reason or "")
