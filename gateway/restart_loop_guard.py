"""Auto-resume restart-loop breaker (defense-3).

Defenses 1-2 (``_HERMES_GATEWAY`` guard on ``hermes gateway stop|restart`` /
``terminal_tool``, cron lifecycle filter) stop the agent scheduling its own restart
but not every SIGTERM source: the supervisor respawns, the gateway auto-resumes the
restart-interrupted session, whose next turn re-runs the offending logic. Boots are persisted to
``<HERMES_HOME>/gateway/restart_loop.json``. One gap-bounded chain spans every boot; the first boot
in it whose predecessor left a recent planned-restart marker is exempt (stored under ``planned``),
and any later planned restart in that chain is counted, preserving the breaker against an
auto-resumed agent that repeatedly requests restart. Tripped → caller SKIPS auto-resume. Any I/O failure fails OPEN, never wedging.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import List, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger("gateway.run")

# The first planned restart in a chain is exempt; subsequent planned restarts count so an
# auto-resumed agent cannot loop forever by repeatedly requesting a graceful restart.
DEFAULT_MAX_RESTARTS = 3
DEFAULT_WINDOW_SECONDS = 60
# Longest gap between consecutive restart-interrupted boots still counted as the
# SAME loop.  A fixed-window prune only sees cycles faster than the window (a slower
# loop drops its history every boot and never trips); chaining on the inter-boot
# gap is period-agnostic, and real quiet resets it.
DEFAULT_MAX_GAP_SECONDS = 300
# Only the newest ``max_restarts`` entries can change a verdict; the rest are forensics.
_MAX_STORED_BOOTS = 50


def _state_path():
    return get_hermes_home() / "gateway" / "restart_loop.json"


def _planned_exit_path():
    return _state_path().with_name("restart_loop_planned_exit.json")


def record_planned_exit() -> None:
    """Carry planned shutdown intent across the process boundary, stamped at exit."""
    with contextlib.suppress(OSError):
        path = _planned_exit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"requested_at": time.time()}, fsync_dir=True)


def consume_planned_restart_signal() -> bool:
    """Consume this PID's service-restart intent and hand it off to the next boot."""
    from gateway.status import consume_planned_restart_marker_for_self
    if not consume_planned_restart_marker_for_self():
        return False
    record_planned_exit()
    return True


def consume_planned_exit(
    window_seconds: int = DEFAULT_WINDOW_SECONDS, *, max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> bool:
    """Take the predecessor's one-shot intent, even when this boot has no sessions to resume."""
    path = _planned_exit_path()
    try:
        recent = planned_marker_is_recent(path, window_seconds=window_seconds, max_gap_seconds=max_gap_seconds)
        path.unlink(missing_ok=True)
        return recent
    except OSError:
        return False


def _load_state() -> tuple[List[float], List[float]]:
    """``(counted boots, exempt planned boots)``; a file without ``planned`` is all counted."""
    try:
        # utf-8-sig: our BOM-tolerant read fix for the persisted boot log.
        raw = _state_path().read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        boots = [float(t) for t in data.get("boots", []) if isinstance(t, (int, float))]
        counted = set(boots)
        # A timestamp in both lists is counted (older writers also listed counted planned boots).
        planned = [
            float(t) for t in data.get("planned", [])
            if isinstance(t, (int, float)) and float(t) not in counted
        ]
        return boots, planned
    except (OSError, ValueError, TypeError, AttributeError):
        return [], []


def _load_boots() -> List[float]:
    return _load_state()[0]


def _save_state(boots: list[float], planned: List[float]) -> None:
    with contextlib.suppress(OSError):
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"boots": boots[-_MAX_STORED_BOOTS:], "planned": planned[-_MAX_STORED_BOOTS:]}),
            encoding="utf-8",
        )


def _chain_gap(window_seconds: int, max_gap_seconds: int) -> float:
    """Inter-boot gap that still links two boots; floored by ``window_seconds`` so
    widening the window never makes the breaker *less* sensitive."""
    return float(max(1, window_seconds, max_gap_seconds))


def _chain_ending_at(boots: list[float], ts: float, gap: float) -> list[float]:
    """Unbroken chain of boots leading up to ``ts`` (oldest first): walks backwards
    while each gap stays within ``gap``; the first wider gap ends the chain (older
    boots are a resolved episode).  Empty when nothing is recent — how a healthy
    gateway forgets a loop."""
    chain: list[float] = []
    prev = ts
    for t in sorted(boots, reverse=True):
        if t > ts:  # clock moved backwards (NTP step, restored file): future entry is adjacent, not a break
            chain.append(t)
            continue
        if prev - t > gap:
            break
        chain.append(t)
        prev = t
    return chain[::-1]


def _current_chain(ts: float, gap: float) -> tuple[List[float], List[float]]:
    """Split the ONE chain over every recorded boot (counted and exempt) into its counted and
    exempt parts.  Chaining each kind separately would double the apparent gap whenever planned
    and unplanned boots alternate, letting a loop slower than ``gap / 2`` evade the breaker."""
    boots, planned = _load_state()
    exempt = set(planned)
    chain = _chain_ending_at(boots + planned, ts, gap)
    return [t for t in chain if t not in exempt], [t for t in chain if t in exempt]


def planned_marker_is_recent(
    path, *, now: Optional[float] = None, window_seconds: int = DEFAULT_WINDOW_SECONDS,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS, use_mtime: bool = False,
) -> bool:
    """True when the planned-restart marker at ``path`` was requested within one chain gap of
    ``now``. The chat /restart routing marker has no timestamp, so its caller uses file mtime.
    A marker can outlive later crashes while a channel stays unreachable; old intent proves nothing.
    Unreadable or undated → False."""
    try:
        requested_at = path.stat().st_mtime if use_mtime else json.loads(
            path.read_text(encoding="utf-8-sig")
        ).get("requested_at")
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if isinstance(requested_at, bool) or not isinstance(requested_at, (int, float)):
        return False
    ts = time.time() if now is None else now
    return abs(ts - float(requested_at)) <= _chain_gap(window_seconds, max_gap_seconds)


def record_restart_interrupted_boot(
    window_seconds: int = DEFAULT_WINDOW_SECONDS, *, now: Optional[float] = None,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> list[float]:
    """Record a counted restart-interrupted boot; return the chain's counted boots + now (most
    recent last).  A persistence failure returns the in-memory list without raising."""
    ts = time.time() if now is None else now
    boots, planned = _current_chain(ts, _chain_gap(window_seconds, max_gap_seconds))
    boots = boots + [ts]
    _save_state(boots, planned)
    return boots


def clear() -> None:
    """Remove the persisted boot log (used on clean shutdown / by tests)."""
    with contextlib.suppress(OSError):
        _state_path().unlink(missing_ok=True)


def check_and_record(
    max_restarts: int = DEFAULT_MAX_RESTARTS, window_seconds: int = DEFAULT_WINDOW_SECONDS, *,
    now: Optional[float] = None, max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
    planned_restart: bool = False,
) -> bool:
    """Record this boot and return True when auto-resume should be skipped.

    Only the first planned restart in a gap-bounded chain (over all boots, planned or not) is
    exempt and does not spend breaker budget.  Every later planned restart in that chain counts,
    so an auto-resumed agent that repeatedly asks the gateway to restart still trips the breaker.
    """
    ts = time.time() if now is None else now
    gap = _chain_gap(window_seconds, max_gap_seconds)
    if planned_restart:
        boots, planned = _current_chain(ts, gap)
        if not planned:
            _save_state(boots, [ts])
            return False
        boots = boots + [ts]
        _save_state(boots, planned)
    else:
        boots = record_restart_interrupted_boot(window_seconds, now=ts, max_gap_seconds=max_gap_seconds)
    tripped = max_restarts > 0 and len(boots) >= max_restarts
    if tripped:
        logger.warning(
            "Restart-loop breaker TRIPPED: %d chained restart-interrupted gateway boots (no gap wider than %ds; "
            "threshold %d). Skipping auto-resume to break a suspected SIGTERM-respawn loop (#30719, #81642). "
            "Restart-interrupted sessions stay resume-pending and will continue on the next real user message. "
            "If this is a false positive, delete %s.",
            len(boots), int(gap), max_restarts, _state_path(),
        )
    return tripped
