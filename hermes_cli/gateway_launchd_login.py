"""One-shot Aqua login trigger: register Background jobs, never run a gateway."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import os
import plistlib
import subprocess


def stop_requested(home: Path) -> bool:
    if (home / "gateway-guardian-stopped").exists():
        return True
    import json
    from datetime import UTC, datetime
    try:
        marker = json.loads((home / ".gateway-planned-stop.json").read_text(encoding="utf-8-sig"))
        pid = marker["target_pid"]
        stamp = datetime.fromisoformat(marker["written_at"])
        age = (datetime.now(UTC) - stamp).total_seconds()
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not 0 <= age <= 60:
            return False
        if os.name == "nt":
            return False
        os.kill(pid, 0)  # windows-footgun: ok — POSIX-only branch; Windows signal zero is unsafe.
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def foreign_guardian(path: Path, home: Path, label: str) -> bool:
    """The guardian is account-shared; only its owning Hermes home may manage it."""
    if not path.exists():
        return False  # An unowned loaded label must still fail closed during preflight.
    body = plistlib.loads(path.read_bytes())
    owner = body.get("EnvironmentVariables", {}).get("HERMES_HOME")
    if body.get("Label") != label or not owner:
        raise RuntimeError(f"cannot prove guardian ownership: {path}")
    return Path(owner).resolve() != home.resolve()


@contextmanager
def lifetime_lock(home: Path, *, blocking: bool = True, announce_wait: bool = False):
    """Shared with CLI/guardian; keep stdlib-only for the isolated login entry point."""
    import fcntl
    path = home / "logs/launchd-lifetime.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not blocking:
                yield False
                return
            if announce_wait:
                print(f"Waiting for lifetime lock: {path}", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield True


def login_bootstrap(home: Path, gateway_plist: Path, *, runner=None, log=None) -> None:
    if log is None:
        from datetime import UTC, datetime
        def timestamped_log(message):
            print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)
        log = timestamped_log
    with lifetime_lock(home):
        _login_bootstrap(home, gateway_plist, runner=runner, log=log)


def _login_bootstrap(home: Path, gateway_plist: Path, *, runner=None, log=print) -> None:
    # Keep this executable stdlib-only so stable current works in isolated mode.
    GUARDIAN_LABEL = "ai.hermes.gateway-guardian"
    run = runner or subprocess.run
    definitions = [(gateway_plist, gateway_plist.stem),
                   (gateway_plist.with_name(f"{GUARDIAN_LABEL}.plist"), GUARDIAN_LABEL)]
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise RuntimeError("launchd login trigger requires a POSIX user")
    uid = getuid()
    domain = f"user/{uid}"
    for path, expected_label in definitions:
        if not path.exists():
            continue
        if expected_label == GUARDIAN_LABEL and foreign_guardian(path, home, GUARDIAN_LABEL):
            log(f"{GUARDIAN_LABEL}: skipped: owned by another Hermes home")
            continue
        definition = plistlib.loads(path.read_bytes())
        label = definition["Label"]
        if (label != expected_label or Path(definition.get("EnvironmentVariables", {}).get("HERMES_HOME", "")).resolve() != home.resolve()
                or definition.get("LimitLoadToSessionType") != "Background"):
            raise RuntimeError(f"refusing non-Background/foreign login target {path}")
        # Stopped intent must suppress the guardian too, not just the traffic writer.
        if stop_requested(home):
            log(f"{label}: skipped: stopped intent")
            continue
        state = run(["launchctl", "print", f"{domain}/{label}"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if state.returncode == 0:
            log(f"{label}: already loaded")
            continue
        if state.returncode not in {3, 113}:
            raise RuntimeError(f"cannot query {domain}/{label}: {state.stderr}")
        # Recheck intent immediately before registration; never exec/kickstart a writer.
        if stop_requested(home):
            log(f"{label}: skipped: stopped intent")
            continue
        opposite = run(["launchctl", "print", f"gui/{uid}/{label}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if opposite.returncode == 0 or opposite.returncode not in {3, 113}:
            raise RuntimeError(f"cannot prove {label} absent from GUI domain; refusing duplicate")
        if stop_requested(home):
            log(f"{label}: skipped: stopped intent")
            continue
        run(["launchctl", "bootstrap", domain, str(path)], check=True, timeout=30)
        verified = run(["launchctl", "print", f"{domain}/{label}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if verified.returncode != 0:
            raise RuntimeError(f"bootstrap did not register {domain}/{label}")
        log(f"{label}: loaded")


def _login_trigger_label(label: str) -> str:
    """Login bootstrap outside ``ai.hermes.gateway*`` and every profile suffix."""
    suffix = label.removeprefix("ai.hermes.gateway")
    return f"ai.hermes.login-bootstrap{suffix}"


def login_plist(home: Path, gateway_plist: Path, label: str) -> bytes:
    from hermes_cli import gateway as gw
    python = home / "current/.venv/bin/python"
    interpreter = str(python) if (home / "current").exists() else gw.get_python_path()
    options = ["--home", str(home), "--gateway-plist", str(gateway_plist)]
    if (home / "current").exists():
        # Stable current aliases follow promotion/GC; no resolved-release path is persisted.
        arguments = [interpreter, "-I", str(home / "current/hermes_cli/gateway_launchd_login.py"), *options]
    else:
        from hermes_cli._launchers import installation_command
        arguments = installation_command(gw.PROJECT_ROOT, options, module="hermes_cli.gateway_launchd_login",
                                         python=interpreter, home=home)
    return plistlib.dumps({
        "Label": _login_trigger_label(label), "RunAtLoad": True, "LimitLoadToSessionType": "Aqua",
        "ProgramArguments": arguments,
        "WorkingDirectory": str(home), "EnvironmentVariables": {"HERMES_HOME": str(home)},
        "StandardOutPath": str(home / "logs/launchd-login.log"),
        "StandardErrorPath": str(home / "logs/launchd-login.error.log"),
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--gateway-plist", type=Path, required=True)
    args = parser.parse_args()
    login_bootstrap(args.home, args.gateway_plist)
