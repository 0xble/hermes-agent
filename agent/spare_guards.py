"""Admission and daily-usage guards for spare fallback routes.

Two guards keep a fallback storm from draining the spare upstreams (default ``meta`` and ``xai``):

* **Concurrency cap** — at most ``delegation.max_concurrent_on_spare`` delegated children may be
  active on one spare upstream at a time, counted across processes through a lease file under
  ``$HERMES_HOME/state``. A lease dies with its owner: released on child exit/restore, dropped when
  its pid is gone, and expiring unless refreshed by the child's API calls.
* **Daily token budget** — ``fallback_budgets.<family>`` caps input + cache-read + output tokens a
  spare upstream serves *as a fallback* per local day.

Both apply only to the fallback walk. A spare that is the role PRIMARY (the quota-burn cron's
deliberate drain) never passes through :func:`reserve_fallback_route`, and usage is recorded only
while ``_provider_fallback_active`` is set.

:func:`resolve_upstream_family` delegates to ``hermes_cli.fallback_config.infer_upstream_family``,
the resolver config validation uses, so a route is one family to both.
"""
from __future__ import annotations

import contextlib
import datetime as _datetime
import errno
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import get_hermes_home
from utils import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_SPARE_UPSTREAMS = ("meta", "xai")
DEFAULT_MAX_CONCURRENT_ON_SPARE = 2
DEFAULT_DAILY_BUDGET_TOKENS = 30_000_000
# A lease is refreshed by every API call the child makes on the spare; a child silent for this
# long (or whose pid is gone) loses it. Tool calls can legitimately run for many minutes, so the
# TTL is a backstop rather than the normal heartbeat.
LEASE_TTL_SECONDS = 3600.0
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_RETRY_SECONDS = 0.05
_STATE_VERSION = 1


@dataclass(frozen=True)
class FallbackRouteReservation:
    """Admission token used to commit or roll back one fallback bind attempt."""

    family: str
    lease: tuple[str, str] | None = None
    prior_lease: tuple[str, str] | None = None
    prior_upstream: str | None = None
    newly_acquired: bool = False

    def __bool__(self) -> bool:
        return True


def _state_path() -> Path:
    return get_hermes_home() / "state" / "spare_fallback_guards.json"


def _local_day() -> str:
    return _datetime.datetime.now().astimezone().date().isoformat()


def _load_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        loaded = load_config_readonly()
    except (ImportError, OSError, TypeError, ValueError):
        logger.debug("Could not load spare fallback guard configuration", exc_info=True)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _positive_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        numeric = float(value)
        if not math.isfinite(numeric):
            return default
        parsed = int(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def spare_settings(config: dict[str, Any] | None = None) -> tuple[frozenset[str], int, dict[str, int]]:
    """``(spare families, concurrency cap, daily budgets)``; a budget <= 0 disables that family's budget."""
    cfg = config if isinstance(config, dict) else _load_config()
    delegation = cfg.get("delegation")
    delegation = delegation if isinstance(delegation, dict) else {}
    raw = delegation.get("spare_upstreams")
    names = raw if isinstance(raw, (list, tuple)) else DEFAULT_SPARE_UPSTREAMS
    spares = frozenset(s for s in (str(n).strip().lower() for n in names) if s)
    cap = _positive_int(delegation.get("max_concurrent_on_spare"), DEFAULT_MAX_CONCURRENT_ON_SPARE)
    budgets = {family: DEFAULT_DAILY_BUDGET_TOKENS for family in spares}
    raw_budgets = cfg.get("fallback_budgets")
    if isinstance(raw_budgets, dict):
        for family, value in raw_budgets.items():
            try:
                if isinstance(value, bool) or not math.isfinite(float(value)):
                    continue
                budgets[str(family).strip().lower()] = int(value)
            except (OverflowError, TypeError, ValueError):
                continue
    return spares, cap, budgets


def resolve_upstream_family(provider: Any, model: Any, entry: dict[str, Any] | None = None) -> str:
    """Upstream family of a route: explicit ``upstream:``, else the model name, else the provider."""
    from hermes_cli.fallback_config import infer_upstream_family
    return infer_upstream_family(provider, model, entry.get("upstream") if isinstance(entry, dict) else None)


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "leases": {}, "usage": {}}


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError):
        logger.warning("Spare fallback guard state unreadable at %s; starting empty", path)
        return _empty_state()
    state = _empty_state()
    if isinstance(raw, dict):
        for key in ("leases", "usage"):
            if isinstance(raw.get(key), dict):
                state[key] = raw[key]
    return state


@contextlib.contextmanager
def _locked_state() -> Iterator[tuple[Path, dict[str, Any]]]:
    """Exclusive cross-process read-modify-write of the guard state."""
    path = _state_path()
    lock_path = path.with_name(".spare_fallback_guards.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ImportError:  # Windows: single-process best effort
            fcntl = None
        if fcntl is not None:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise OSError("timed out acquiring spare fallback guard lock") from exc
                    time.sleep(_LOCK_RETRY_SECONDS)
        try:
            yield path, _read_state(path)
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(state, sort_keys=True), mode=0o600)


def _pid_alive(pid: Any) -> bool:
    from gateway.status import _pid_exists

    try:
        return _pid_exists(int(pid))
    except (OSError, TypeError, ValueError):
        return True


def _prune(state: dict[str, Any], now: float, day: str) -> None:
    """Drop expired/orphaned leases and every usage day but today (the rollover)."""
    for family, rows in list(state["leases"].items()):
        live = {}
        if isinstance(rows, dict):
            for token, row in rows.items():
                if not isinstance(row, dict):
                    continue
                try:
                    fresh = math.isfinite(float(row["expires_at"])) and float(row["expires_at"]) > now
                except (KeyError, TypeError, ValueError, OverflowError):
                    fresh = False
                if fresh and _pid_alive(row.get("pid")):
                    live[token] = row
        if live:
            state["leases"][family] = live
        else:
            state["leases"].pop(family, None)
    state["usage"] = {d: v for d, v in state["usage"].items() if d == day and isinstance(v, dict)}


def _used_today(state: dict[str, Any], day: str, family: str) -> int:
    rows = state.get("usage", {}).get(day)
    raw = rows.get(family, 0) if isinstance(rows, dict) else 0
    try:
        numeric = float(raw)
        if not math.isfinite(numeric):
            return 0
        return max(0, int(raw))
    except (OverflowError, TypeError, ValueError):
        return 0


def _in_delegated_child() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context
        return bool(is_delegated_child_context())
    except (ImportError, OSError, TypeError, ValueError):
        logger.debug("Could not determine delegated-child context", exc_info=True)
        return False


def _held_lease(agent: Any) -> tuple[str, str] | None:
    lease = getattr(agent, "_spare_fallback_lease", None)
    if isinstance(lease, (tuple, list)) and len(lease) == 2:
        return str(lease[0]), str(lease[1])
    return None


def _log_once(agent: Any, key: str, message: str, *args: Any) -> None:
    seen = getattr(agent, "_spare_guard_logged", None)
    if not isinstance(seen, set):
        seen = set()
        with contextlib.suppress(Exception):
            agent._spare_guard_logged = seen
    if key not in seen:
        seen.add(key)
        logger.warning(message, *args)


def reserve_fallback_route(agent: Any, entry: dict[str, Any]) -> FallbackRouteReservation | None:
    """Admit *entry* and return a token for the bind attempt, or ``None`` to skip it.

    The token keeps a prior spare lease alive until the route bind succeeds. This is important when
    a child rotates from one spare to another: a failed bind must not release the lease it was already
    holding.
    """
    family = resolve_upstream_family(entry.get("provider"), entry.get("model"), entry)
    spares, cap, budgets = spare_settings()
    held = _held_lease(agent)
    prior_upstream = getattr(agent, "_spare_fallback_upstream", None)
    if family not in spares:
        return FallbackRouteReservation(family, prior_lease=held, prior_upstream=prior_upstream)

    day, now, budget = _local_day(), time.time(), budgets.get(family, 0)
    delegated = _in_delegated_child()
    lease = None
    newly_acquired = False
    try:
        with _locked_state() as (path, state):
            _prune(state, now, day)
            if budget > 0 and _used_today(state, day, family) >= budget:
                _write_state(path, state)
                _log_once(agent, f"budget:{family}",
                          "Fallback skip: spare upstream %s is over its daily token budget (%d)", family, budget)
                return None
            if delegated:
                rows = state["leases"].setdefault(family, {})
                if held and held[0] == family and held[1] in rows:
                    rows[held[1]]["expires_at"] = now + LEASE_TTL_SECONDS
                    lease = held
                elif len(rows) >= cap:
                    if not rows:
                        state["leases"].pop(family, None)
                    _write_state(path, state)
                    _log_once(agent, f"cap:{family}",
                              "Fallback skip: spare upstream %s already has %d/%d delegated children", family, len(rows), cap)
                    return None
                else:
                    token = uuid.uuid4().hex
                    lease = (family, token)
                    rows[token] = {"pid": os.getpid(), "expires_at": now + LEASE_TTL_SECONDS}
                    newly_acquired = True
            _write_state(path, state)
    except OSError:
        # Guard state is advisory: a filesystem or lock failure must not turn a provider fallback
        # into a terminal error.
        logger.debug("Spare fallback admission state unavailable; allowing route", exc_info=True)

    if newly_acquired:
        agent._spare_fallback_lease = lease
    agent._spare_fallback_upstream = family
    return FallbackRouteReservation(
        family, lease=lease, prior_lease=held, prior_upstream=prior_upstream,
        newly_acquired=newly_acquired,
    )


def fallback_route_admissible(agent: Any, entry: dict[str, Any]) -> bool:
    """Whether ``reserve_fallback_route`` would admit *entry* now. Reads only: no lease is
    taken and nothing is written, so a caller can ask before it commits to a switch."""
    family = resolve_upstream_family(entry.get("provider"), entry.get("model"), entry)
    spares, cap, budgets = spare_settings()
    if family not in spares:
        return True
    day, now, budget = _local_day(), time.time(), budgets.get(family, 0)
    try:
        with _locked_state() as (_path, state):
            _prune(state, now, day)  # on the in-memory copy only
            if budget > 0 and _used_today(state, day, family) >= budget:
                return False
            if not _in_delegated_child():
                return True
            rows = state["leases"].get(family) or {}
            held = _held_lease(agent)
            return bool(held and held[0] == family and held[1] in rows) or len(rows) < cap
    except OSError:
        return True  # advisory state, same as the reservation: unreadable admits


def _remove_lease(lease: tuple[str, str]) -> None:
    family, token = lease
    try:
        with _locked_state() as (path, state):
            rows = state["leases"].get(family)
            if isinstance(rows, dict) and rows.pop(token, None) is not None:
                if not rows:
                    state["leases"].pop(family, None)
                _write_state(path, state)
    except OSError:
        logger.debug("Could not release spare fallback lease", exc_info=True)


def commit_fallback_route(agent: Any, reservation: FallbackRouteReservation | None) -> None:
    """Commit a successfully bound route and retire any lease from its prior route."""
    if not isinstance(reservation, FallbackRouteReservation):
        return
    if reservation.prior_lease and reservation.prior_lease != reservation.lease:
        _remove_lease(reservation.prior_lease)
    if reservation.family not in spare_settings()[0]:
        agent._spare_fallback_lease = None
        agent._spare_fallback_upstream = None
    else:
        agent._spare_fallback_lease = reservation.lease
        agent._spare_fallback_upstream = reservation.family


def release_fallback_route(
    agent: Any, *, _lease: tuple[str, str] | FallbackRouteReservation | None = None,
) -> None:
    """Return a child lease, or roll back one failed bind using its admission token."""
    if isinstance(_lease, FallbackRouteReservation):
        if _lease.newly_acquired and _lease.lease:
            _remove_lease(_lease.lease)
        agent._spare_fallback_lease = _lease.prior_lease
        agent._spare_fallback_upstream = _lease.prior_upstream
        return
    lease = _lease or _held_lease(agent)
    if lease is None:
        return
    if _lease is None:
        agent._spare_fallback_lease = None
        agent._spare_fallback_upstream = None
    _remove_lease(lease)


def record_fallback_usage(agent: Any, usage: Any) -> None:
    """Add one API call's input + cache-read + output tokens to the active spare's daily total."""
    family = getattr(agent, "_spare_fallback_upstream", None)
    if not family or not getattr(agent, "_provider_fallback_active", False):
        return
    total = 0
    for field in ("input_tokens", "cache_read_tokens", "output_tokens"):
        with contextlib.suppress(OverflowError, TypeError, ValueError):
            total += max(0, int(getattr(usage, field, 0) or 0))
    held = _held_lease(agent)
    if total <= 0 and not held:
        return
    day, now = _local_day(), time.time()
    try:
        with _locked_state() as (path, state):
            _prune(state, now, day)
            if total > 0:
                state["usage"].setdefault(day, {})[family] = _used_today(state, day, family) + total
            if held:  # liveness heartbeat for the concurrency lease
                row = (state["leases"].get(held[0]) or {}).get(held[1])
                if isinstance(row, dict):
                    row["expires_at"] = now + LEASE_TTL_SECONDS
            _write_state(path, state)
    except OSError:
        logger.debug("Could not record spare fallback usage", exc_info=True)


def used_today(family: str) -> int:
    try:
        with _locked_state() as (_, state):
            return _used_today(state, _local_day(), family)
    except OSError:
        logger.debug("Could not read spare fallback usage", exc_info=True)
        return 0
