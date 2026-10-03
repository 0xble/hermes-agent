"""Pinned launchd definitions for opt-in gateway generations."""
from __future__ import annotations

from pathlib import Path
import plistlib
import subprocess
import time
import uuid


def generation_launchd_label(slot: str) -> str:
    """Render a UUID label, retaining legacy slot names for the flag-off route."""
    normalized = str(slot).strip().lower()
    if normalized in {"a", "b"}:
        return f"ai.hermes.gateway-{normalized}"  # Pre-amendment flag-off rendering.
    return f"ai.hermes.gateway.g-{uuid.UUID(normalized).hex}"


def render_generation_launchd_plist(*, slot: str, release_sha: str, release_root: Path,
                                     interpreter: Path, hermes_home: Path,
                                     standby: bool = True) -> str:
    """Render a pinned generation plist without consulting ``current`` or live gateway state."""
    label = generation_launchd_label(slot)
    from gateway.generation import forward_only_handover_enabled
    from hermes_cli.config_effective import load_user_config_effective
    forward_only = forward_only_handover_enabled(load_user_config_effective(Path(hermes_home) / 'config.yaml', fail_closed=True))
    if forward_only and not label.startswith('ai.hermes.gateway.g-'):
        raise ValueError('forward-only generations require a full UUID label')
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
            "HERMES_GENERATION_SCOPE": uuid.uuid4().hex,
        },
        # SuccessfulExit implies RunAtLoad, even when RunAtLoad is false.
        "RunAtLoad": not (forward_only and standby),
        "KeepAlive": False if forward_only and standby else {"SuccessfulExit": False},
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")


def bootstrap_generation_plist(*, domain: str, plist_path: Path, label: str,
                               runner=None, timeout: float = 30, before_launch=None) -> None:
    """Bootstrap once; never bootout an existing generation on an EIO collision."""
    deadline = time.monotonic() + max(float(timeout), 0.0)
    command = ["launchctl", "bootstrap", domain, str(plist_path)]
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        return value
    payload = plistlib.loads(Path(plist_path).read_bytes())
    from gateway.generation import forward_only_handover_enabled
    from hermes_cli.config_effective import load_user_config_effective
    home = Path(payload['EnvironmentVariables']['HERMES_HOME'])
    if payload.get('Label') != label:
        raise ValueError('generation plist label differs')
    if (forward_only_handover_enabled(load_user_config_effective(home / 'config.yaml', fail_closed=True))
            and not label.startswith('ai.hermes.gateway.g-')):
        raise ValueError('forward-only generations require a full UUID label')
    if label.startswith("ai.hermes.gateway.g-"):
        generation_id = uuid.UUID(label.removeprefix("ai.hermes.gateway.g-"))
        if generation_launchd_label(str(generation_id)) != label:
            raise ValueError("generation label must carry the full UUID")
        from gateway.generation import GenerationCoordinator
        coordinator = GenerationCoordinator(home)
        if not any(row["label"] == label and row["state"] == "standby" and row["pid"] is None
                   for row in coordinator.generations()):
            raise RuntimeError("reserve the generation before bootstrap")
    elif label not in {"ai.hermes.gateway-a", "ai.hermes.gateway-b"}:
        raise ValueError("only reserved generation labels may be bootstrapped")
    refresh_generation_scope(plist_path)
    remaining()
    if before_launch is not None:
        before_launch(plistlib.loads(Path(plist_path).read_bytes())['EnvironmentVariables']['HERMES_GENERATION_SCOPE'])
    remaining_time = remaining()
    (runner or subprocess.run)(command,
                               check=True, timeout=min(30, remaining_time))
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired(command, timeout)


def refresh_generation_scope(plist_path: Path) -> None:
    """Write a fresh nonce at bootstrap, leaving KeepAlive respawns in that scope."""
    from hermes_cli.immutable_releases import _atomic_bytes
    path = Path(plist_path)
    payload = plistlib.loads(path.read_bytes())
    payload.setdefault("EnvironmentVariables", {})["HERMES_GENERATION_SCOPE"] = uuid.uuid4().hex
    _atomic_bytes(path, plistlib.dumps(payload))
