"""Topical helpers extracted from hermes_cli/update_cmd_fleet.py."""
from __future__ import annotations

def _service_restart_sec(scope_cmd_: list, svc_name_: str, default: float = 0.0) -> float:
    """Read the unit's ``RestartUSec`` in seconds. ``is-active`` pollers must wait
    >= RestartSec + slack or they give up *during* the cooldown and misreport."""
    from hermes_cli.update_cmd_fleet import (_RESTART_SEC_UNITS, _systemctl, subprocess, suppress)
    try:
        _show = _systemctl(scope_cmd_ + ["show", svc_name_, "--property=RestartUSec", "--value"], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return default
    raw = (_show.stdout or "").strip()
    # Values like "30s", "100ms", "1min 30s", "infinity"; on any miss return default.
    if not raw or raw == "infinity":
        return default
    total = 0.0
    matched = False
    for part in raw.split():
        for _suf, _mult in _RESTART_SEC_UNITS:
            if part.endswith(_suf):
                with suppress(ValueError):
                    total += float(part[: -len(_suf)]) * _mult
                    matched = True
                break
    return total if matched else default
