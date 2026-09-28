from __future__ import annotations

"""Pinned launchd definitions for an opt-in overlapping gateway generation."""

from pathlib import Path
import plistlib
import subprocess


# Overlap helpers are intentionally separate from the legacy renderer above. They are pure and
# can be used by the updater without changing the installed gateway definition.
def generation_launchd_label(slot: str) -> str:
    """Return the alternating generation label used by overlap handover."""
    normalized = str(slot).strip().lower()
    if normalized not in {"a", "b"}:
        raise ValueError("generation slot must be 'a' or 'b'")
    return f"ai.hermes.gateway-{normalized}"


def render_generation_launchd_plist(*, slot: str, release_sha: str, release_root: Path,
                                     interpreter: Path, hermes_home: Path,
                                     standby: bool = True) -> str:
    """Render a pinned generation plist without consulting ``current`` or live gateway state."""
    label = generation_launchd_label(slot)
    release = Path(release_root).resolve(strict=True)
    python = Path(interpreter).absolute()
    if python != release / ".venv" / "bin" / "python" or not python.is_file():
        raise ValueError("generation interpreter must be pinned inside release root")
    args = [str(python), "-m", "hermes_cli.main", "gateway", "run"]
    if standby:
        args.append("--standby")
    payload = {
        "Label": label,
        "ProgramArguments": args,
        "WorkingDirectory": str(release),
        "EnvironmentVariables": {
            "HERMES_HOME": str(Path(hermes_home).resolve()),
            "PYTHONPATH": str(release),
            "HERMES_RELEASE_SHA": str(release_sha),
            "HERMES_LAUNCHD_LABEL": label,
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")


def bootstrap_generation_plist(*, domain: str, plist_path: Path, label: str) -> None:
    """Bootstrap once; never bootout an existing generation on an EIO collision."""
    if not label.startswith("ai.hermes.gateway-") or label.rsplit("-", 1)[-1] not in {"a", "b"}:
        raise ValueError("only alternating generation labels may be bootstrapped")
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)],
                   check=True, timeout=30)
