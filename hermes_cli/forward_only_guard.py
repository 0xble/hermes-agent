"""Fail-closed detection of state left by the withdrawn forward-only handover."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

LEFTOVERS_DOC = "maintenance/seamless-restart.md#leftovers-after-withdrawal"
_FORWARD_LABEL = re.compile(r"\b(ai\.hermes\.gateway\.g-[0-9a-fA-F]{32})\b")
_TERMINAL_OUTCOMES = frozenset({"success", "rolled_back", "refused", "aborted"})


def _loaded_forward_labels(*, runner=None) -> list[str]:
    """Read launchd's loaded-job inventory without changing service state."""
    if sys.platform != "darwin":
        return []
    runner = runner or subprocess.run
    try:
        result = runner(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not inspect loaded launchd jobs: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown launchctl error").strip()
        raise RuntimeError(f"could not inspect loaded launchd jobs: {detail}")
    return sorted(set(_FORWARD_LABEL.findall(result.stdout or "")))


def _forward_update_leftover(home: Path) -> bool:
    path = home / "forward-update.json"
    if not path.exists():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return True
    if not isinstance(record, dict):
        return True
    outcome = record.get("outcome", record.get("state"))
    return outcome not in _TERMINAL_OUTCOMES or bool(record.get("bookkeeping_pending"))


def leftover_forward_only_state(home: Path) -> list[str]:
    """Return human-readable leftover descriptions; inspection is read-only and fail-closed."""
    home = Path(home)
    findings = [f"file {home / 'forward-update.json'}"
                ] if _forward_update_leftover(home) else []
    findings.extend(f"loaded launchd label {label}" for label in _loaded_forward_labels())
    return findings


def refuse_if_forward_only_leftovers(home: Path) -> None:
    findings = leftover_forward_only_state(home)
    if findings:
        joined = "; ".join(findings)
        raise RuntimeError(
            "refusing to continue because withdrawn forward-only state remains: "
            f"{joined}. See the 'Leftovers after withdrawal' section at {LEFTOVERS_DOC}; "
            "no automatic cleanup is performed."
        )


__all__ = ["LEFTOVERS_DOC", "leftover_forward_only_state", "refuse_if_forward_only_leftovers"]
