"""When heavy background deletes may run.

Dependency-install orphan collection and the periodic worktree pruner each delete tens of
thousands of files per pass. On a nearly full APFS volume a daytime burst of deletes, on top of
the volume's own reclamation, stalled every process writing to it (state.db included) for minutes
on 2026-10-11. Both run only inside ``maintenance.deletion_window`` (local time, ``HH:MM-HH:MM``,
may wrap midnight). An empty value allows any time.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DELETION_WINDOW = "02:00-05:00"
_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def window_contains(spec: object, at: datetime) -> bool:
    """Whether *at* falls in *spec*. Empty/None means always; an invalid spec uses the default."""
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return True
    match = _WINDOW_RE.match(spec) if isinstance(spec, str) else None
    if match is None or int(match[1]) > 23 or int(match[3]) > 24 or int(match[2]) > 59 or int(match[4]) > 59:
        logger.warning("maintenance.deletion_window %r is not HH:MM-HH:MM; using %s", spec, DEFAULT_DELETION_WINDOW)
        match = _WINDOW_RE.match(DEFAULT_DELETION_WINDOW)
    start = int(match[1]) * 60 + int(match[2])
    end = int(match[3]) * 60 + int(match[4])
    minute = at.hour * 60 + at.minute
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def deletion_window_open(at: Optional[datetime] = None) -> bool:
    """Whether the active profile's deletion window is open now (or at *at*)."""
    try:
        from hermes_cli.config import load_config_readonly

        spec = (load_config_readonly().get("maintenance") or {}).get("deletion_window", DEFAULT_DELETION_WINDOW)
    except Exception:
        spec = DEFAULT_DELETION_WINDOW
    if at is None:
        import hermes_time

        at = hermes_time.now()
    return window_contains(spec, at)
