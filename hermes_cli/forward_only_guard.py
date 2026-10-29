"""Fail-closed detection of state left by the withdrawn forward-only handover."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

# ``timeout_for(cap)`` returns one launchctl probe's timeout. A bounded caller (the guardian)
# passes its remaining deadline; everyone else gets the fixed cap.
TimeoutFor = Callable[[float], float]


def _fixed(cap: float) -> float:
    return cap

LEFTOVERS_DOC = "maintenance/seamless-restart.md#leftovers-after-withdrawal"
_FORWARD_LABEL = re.compile(r"\b(ai\.hermes\.gateway\.g-[0-9a-fA-F]{32})\b")
_TERMINAL_OUTCOMES = frozenset({"success", "rolled_back", "refused", "aborted"})


class LeftoverInspectionError(RuntimeError):
    """launchd's inventory could not be read, so leftover state is unknown (not known present)."""


def _loaded_forward_labels(*, runner=None, timeout_for: TimeoutFor = _fixed) -> list[str]:
    """Read launchd's loaded-job inventory without changing service state."""
    if sys.platform != "darwin":
        return []
    runner = runner or subprocess.run
    timeout = timeout_for(5)
    try:
        result = runner(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LeftoverInspectionError(f"could not inspect loaded launchd jobs: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown launchctl error").strip()
        raise LeftoverInspectionError(f"could not inspect loaded launchd jobs: {detail}")
    return sorted(set(_FORWARD_LABEL.findall(result.stdout or "")))


_HOME_LINE = re.compile(r"^\s*HERMES_HOME\s*=>\s*(.+?)\s*$", re.MULTILINE)
_LAUNCHCTL_SERVICE_NOT_FOUND = "Could not find service"


def _classify_launchctl_print(result) -> str:
    """Classify one ``launchctl print`` result as FOUND, ABSENT, or ERROR."""
    if result.returncode == 0:
        return "FOUND"
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    if _LAUNCHCTL_SERVICE_NOT_FOUND in output:
        return "ABSENT"
    return "ERROR"


def _label_hermes_homes(label: str, *, runner=None, timeout_for: TimeoutFor = _fixed) -> list[Path] | None:
    """HERMES_HOME values a loaded generation job was rendered for, or None when unreadable.

    Generation plists pin ``EnvironmentVariables.HERMES_HOME`` to the resolved home, and
    ``launchctl print`` echoes it in the job's ``environment`` block. A label can be managed by
    either the Aqua ``gui/<uid>`` domain or the background ``user/<uid>`` domain, so inspect both.
    Each domain probe is classified as FOUND (return code 0), ABSENT when
    ``Could not find service`` appears in its output, or ERROR (an exception or any other
    nonzero result).
    """
    import os

    if sys.platform != "darwin":
        return None
    runner = runner or subprocess.run
    uid = os.getuid()  # windows-footgun: ok (darwin-gated above)
    domains = (f"gui/{uid}", f"user/{uid}")
    errors = []
    owners = []
    missing_home = False
    for domain in domains:
        timeout = timeout_for(5)
        try:
            result = runner(
                ["launchctl", "print", f"{domain}/{label}"],  # windows-footgun: ok (darwin-gated above)
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
            classification = _classify_launchctl_print(result)
        except Exception as exc:
            errors.append(f"{domain}: {exc}")
            continue
        if classification == "ERROR":
            detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
            errors.append(f"{domain}: {detail}")
            continue
        if classification == "ABSENT":
            continue
        match = _HOME_LINE.search(result.stdout or "")
        if match:
            owners.append(Path(match.group(1)).expanduser())
        else:
            missing_home = True

    if errors:
        detail = "; ".join(errors)
        raise LeftoverInspectionError(f"could not inspect launchd owner for {label}: {detail}")
    if missing_home:
        return None
    return owners


def _same_home(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


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


def leftover_forward_only_state(home: Path, *, timeout_for: TimeoutFor = _fixed) -> list[str]:
    """Return human-readable leftover descriptions; inspection is read-only and fail-closed."""
    home = Path(home)
    findings = [f"file {home / 'forward-update.json'}"
                ] if _forward_update_leftover(home) else []
    for label in _loaded_forward_labels(timeout_for=timeout_for):
        owners = _label_hermes_homes(label, timeout_for=timeout_for)
        # Another installation's generation job is not this home's leftover only when every
        # readable domain points elsewhere. An unreadable owner stays fail-closed: refusing is
        # recoverable, a second poller on one token is not.
        if owners is not None and all(not _same_home(owner, home) for owner in owners):
            continue
        suffix = "" if owners is not None else " (owner HERMES_HOME unreadable)"
        findings.append(f"loaded launchd label {label}{suffix}")
    return findings


def refuse_if_forward_only_leftovers(home: Path, *, timeout_for: TimeoutFor = _fixed) -> None:
    findings = leftover_forward_only_state(home, timeout_for=timeout_for)
    if findings:
        joined = "; ".join(findings)
        raise RuntimeError(
            "refusing to continue because withdrawn forward-only state remains: "
            f"{joined}. See the 'Leftovers after withdrawal' section at {LEFTOVERS_DOC}; "
            "no automatic cleanup is performed."
        )


__all__ = ["LEFTOVERS_DOC", "LeftoverInspectionError", "leftover_forward_only_state", "refuse_if_forward_only_leftovers"]
