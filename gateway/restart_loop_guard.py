"""Auto-resume restart-loop breaker (defense-3).

Defenses 1-2 (``_HERMES_GATEWAY`` guard on ``hermes gateway stop|restart`` /
``terminal_tool``, cron lifecycle filter) stop the agent scheduling its own restart
but not every SIGTERM source: the supervisor respawns, the gateway auto-resumes the
restart-interrupted session, whose next turn re-runs the offending logic. Boots are persisted to
``<HERMES_HOME>/gateway/restart_loop.json``. The first boot whose predecessor left a planned-restart
marker is recorded separately from the crash chain; a second planned restart in the same
gap-bounded chain is counted, preserving the breaker against an auto-resumed agent that repeatedly
requests restart. Tripped → caller SKIPS auto-resume. Any I/O failure fails OPEN, never wedging.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import List, Optional

from hermes_constants import get_hermes_home

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


def _load_state() -> tuple[List[float], List[float]]:
    try:
        # utf-8-sig: our BOM-tolerant read fix for the persisted boot log.
        raw = _state_path().read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        boots = data.get("boots", [])
        planned = data.get("planned", [])
        return (
            [float(t) for t in boots if isinstance(t, (int, float))],
            [float(t) for t in planned if isinstance(t, (int, float))],
        )
    except (OSError, ValueError, TypeError):
        return [], []


def _load_boots() -> List[float]:
    return _load_state()[0]


def _save_state(boots: List[float], planned: List[float]) -> None:
    with contextlib.suppress(OSError):
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"boots": boots, "planned": planned}), encoding="utf-8")


def _save_boots(boots: List[float]) -> None:
    _save_state(boots, _load_state()[1])


def _chain_gap(window_seconds: int, max_gap_seconds: int) -> float:
    """Inter-boot gap that still links two boots; floored by ``window_seconds`` so
    widening the window never makes the breaker *less* sensitive."""
    return float(max(1, window_seconds, max_gap_seconds))


def _chain_ending_at(boots: List[float], ts: float, gap: float) -> List[float]:
    """Unbroken chain of boots leading up to ``ts`` (oldest first): walks backwards
    while each gap stays within ``gap``; the first wider gap ends the chain (older
    boots are a resolved episode).  Empty when nothing is recent — how a healthy
    gateway forgets a loop."""
    chain: List[float] = []
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


def record_restart_interrupted_boot(
    window_seconds: int = DEFAULT_WINDOW_SECONDS, *, now: Optional[float] = None,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> List[float]:
    """Record a restart-interrupted boot; return the pruned chain + now (most recent
    last).  A persistence failure returns the in-memory list without raising."""
    ts = time.time() if now is None else now
    boots = _chain_ending_at(_load_boots(), ts, _chain_gap(window_seconds, max_gap_seconds)) + [ts]
    _save_boots(boots[-_MAX_STORED_BOOTS:])
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

    The first planned restart in a contiguous chain is kept as a separate marker instead of
    spending breaker budget. A second planned restart in that same chain is counted, so an
    auto-resumed agent that repeatedly asks the gateway to restart still trips the breaker.
    """
    ts = time.time() if now is None else now
    gap = _chain_gap(window_seconds, max_gap_seconds)
    boots, planned = _load_state()
    boots = _chain_ending_at(boots, ts, gap)
    planned = _chain_ending_at(planned, ts, gap)
    if planned_restart and not planned:
        planned = [ts]
        _save_state(boots[-_MAX_STORED_BOOTS:], planned[-_MAX_STORED_BOOTS:])
        return False
    boots = boots + [ts]
    if planned_restart:
        planned = planned + [ts]
    _save_state(boots[-_MAX_STORED_BOOTS:], planned[-_MAX_STORED_BOOTS:])
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
