"""Candidate selection for resumed sessions, including Relay fronted platforms."""
import logging
from typing import Any, Optional

from gateway.config import Platform

logger = logging.getLogger("gateway.run")


def resume_pending_candidates(runner: Any, platform: Optional[Platform] = None, *,
                              record_boot: bool = True) -> Optional[list]:
    """Snapshot resume-pending entries; only the boot path spends breaker budget."""
    from gateway.run_restart_recovery import ResumeCandidates
    relay = runner._primary_adapters().get(Platform.RELAY) if platform == Platform.RELAY else None
    fronts = getattr(relay, "fronts_platform", None)
    try:
        with runner.session_store._lock:
            runner.session_store._ensure_loaded_locked()
            candidates = ResumeCandidates([
                entry for entry in runner.session_store._entries.values()
                if entry.resume_pending
                and not entry.suspended
                and entry.origin is not None
                and entry.resume_reason in runner._AUTO_RESUME_REASONS
                and (platform is None or getattr(entry.origin, "platform", None) == platform
                     or (callable(fronts) and fronts(entry.origin.platform)))
            ])
    except Exception as exc:
        logger.warning("Failed to enumerate resume-pending sessions: %s", exc, exc_info=True)
        return None
    # Only boots with restart-interrupted sessions count. Shutdown stamps one-shot planned
    # intent after draining; recent chat/home notification markers remain a fallback.
    # The guard gives the first planned boot one chain-local allowance; repeated planned boots count.
    candidates.breaker_tripped = bool(getattr(runner, "_restart_resume_breaker_tripped", False))
    if record_boot:
        try:
            from gateway import restart_loop_guard as guard
            from gateway.run import _hermes_home, _planned_restart_notification_path
            max_restarts, window, max_gap = runner._restart_loop_guard_config()
            planned_exit = guard.consume_planned_exit(window, max_gap_seconds=max_gap)
            if not candidates:
                return candidates
            planned_restart = planned_exit or guard.planned_marker_is_recent(
                _hermes_home / ".restart_notify.json", window_seconds=window,
                max_gap_seconds=max_gap, use_mtime=True,
            ) or guard.planned_marker_is_recent(
                _planned_restart_notification_path(), window_seconds=window, max_gap_seconds=max_gap,
            )
            candidates.breaker_tripped = guard.check_and_record(
                max_restarts, window, max_gap_seconds=max_gap, planned_restart=planned_restart,
            )
        except Exception as exc:
            logger.debug("Restart-loop guard check skipped: %s", exc, exc_info=True)
    return candidates
