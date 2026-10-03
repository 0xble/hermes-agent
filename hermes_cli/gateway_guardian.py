"""One-shot macOS launchd guardian for an immutable Hermes gateway release.

The job is independent of the gateway process tree. It never updates source, retries
work, or falls back to the checkout when the release pointer is damaged.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import uuid
import hermes_yaml as yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.immutable_releases import ReleasePaths, _release_is_ready, rollback
from hermes_cli.gateway_forward_update import STARTUP_SECONDS
from gateway.deadline import deadline_scope, with_deadline_scope
from gateway import deadline as gateway_deadline

GUARDIAN_LABEL = "ai.hermes.gateway-guardian"
INTERVAL = 30
MAX_REPAIRS = 3


def _domain(label: str) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("gateway guardian requires macOS launchd")
    from hermes_cli.gateway_launchd import _probe_launchd_domain_for_label
    return _probe_launchd_domain_for_label(label)


def _gateway_domain(label: str, preferred: str | None, *, runner=None, timeout: float = 10) -> str:
    """Observe both domains before trusting a saved domain or starting an unloaded job."""
    deadline = gateway_deadline.now() + max(0.0, timeout)
    enclosing = gateway_deadline.current()
    if enclosing is not None:
        deadline = min(deadline, enclosing)
    domains = (f"gui/{os.getuid()}", f"user/{os.getuid()}")  # windows-footgun: ok (macOS launchd only)
    if preferred is not None and preferred not in domains:
        raise RuntimeError("guardian domain is not a gateway launchd domain for this user")
    states = {}
    for candidate in domains:
        try:
            remaining = deadline - gateway_deadline.now()
            if remaining <= 0:
                raise RuntimeError("gateway domain probe deadline exceeded")
            states[candidate] = _launch_state(candidate, label, runner=runner, timeout=min(5, remaining))
        except RuntimeError:
            states[candidate] = "unknown"
    if gateway_deadline.now() >= deadline:
        raise RuntimeError("gateway domain probe deadline exceeded")
    loaded = [candidate for candidate, state in states.items() if state in {"loaded", "parked"}]
    if len(loaded) > 1:
        raise RuntimeError("gateway label is loaded in both launchd domains")
    if loaded:
        return loaded[0]
    if "unknown" in states.values():
        raise RuntimeError("cannot prove gateway unloaded in both launchd domains")
    if preferred is not None:
        return preferred
    remaining = deadline - gateway_deadline.now()
    if remaining <= 0:
        raise RuntimeError("gateway domain probe deadline exceeded")
    try:
        result = (runner or subprocess.run)(
            ["launchctl", "managername"], capture_output=True, text=True,
            encoding="utf-8", timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        return domains[1]
    return domains[0] if "Aqua" in (result.stdout or "") else domains[1]


def intent_path(home: Path) -> Path:
    return home / "gateway-guardian-stopped"


def set_intent(home: Path, *, stopped: bool) -> None:
    path = intent_path(home)
    if stopped:
        path.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def receipt(home: Path, action: str, outcome: str, **detail: object) -> Path:
    directory = home / "logs" / "guardian"
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - 3600
    payload = {"at": now.isoformat(), "action": action, "outcome": outcome, **detail}
    for prior in directory.glob("*.json"):
        try:
            if prior.stat().st_mtime < cutoff:
                prior.unlink()
            elif outcome in {"alert", "capped"}:
                old = json.loads(prior.read_text(encoding="utf-8-sig"))
                if {k: v for k, v in old.items() if k != "at"} == {k: v for k, v in payload.items() if k != "at"}:
                    return prior
        except (OSError, ValueError):
            continue
    path = directory / f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def enabled(home: Path) -> bool:
    from hermes_cli.config_effective import load_user_config_effective
    config = load_user_config_effective(home / "config.yaml", fail_closed=True)
    value = (config.get("gateway") or {}).get("guardian", {}).get("enabled", False)
    if type(value) is not bool:
        raise ValueError("gateway.guardian.enabled must be a boolean")
    return value


def _switch(home: Path, *, grace: float) -> tuple[str, dict | None]:
    for name in ("release-txn.json", "release-last-txn.json"):
        path = home / name
        if not path.exists():
            continue
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ValueError(f"invalid release receipt: {path}")
        if record.get("operation") not in {"promote", "first-migration"} or record.get("reload_ack"):
            return "none", None
        issued = record.get("reload_issued")
        if not isinstance(issued, dict) or not isinstance(issued.get("at"), str):
            return "none", None
        age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(issued["at"]).timestamp()
        if age < grace:
            return "waiting", None
        return "expired", record
    return "none", None


def _remaining(deadline: float, cap: float) -> float:
    remaining = deadline - gateway_deadline.now()
    if remaining <= 0:
        raise RuntimeError("guardian repair deadline exceeded")
    return min(cap, remaining)


def _supervised_pid(label: str, *, runner=None, timeout: float = 10) -> int | None:
    from hermes_cli.gateway_launchd import _launchctl_supervised_pid
    try:
        if runner is None:
            return _launchctl_supervised_pid(label, timeout=timeout)
        return _launchctl_supervised_pid(label, runner=runner, timeout=timeout)
    except TypeError as exc:
        # Keep older injected test doubles source-compatible; the production helper
        # accepts the deadline-bound timeout above.
        if "timeout" not in str(exc):
            raise
        return (_launchctl_supervised_pid(label) if runner is None else
                _launchctl_supervised_pid(label, runner=runner))


@with_deadline_scope
def healthy(home: Path, label: str, expected: Path, runner=None, deadline: float | None = None) -> bool:
    from gateway.status import read_runtime_status, runtime_status_is_stale
    import psutil
    state = read_runtime_status(home / "gateway_state.json") or {}
    pid = state.get("pid")
    if type(pid) is not int or state.get("gateway_state") not in {"running", "degraded"}:
        return False
    if state.get("code_sha") != expected.name or runtime_status_is_stale(state):
        return False
    try:
        timeout = _remaining(deadline, 10) if deadline is not None else 10
        supervised = _supervised_pid(label, runner=runner, timeout=timeout)
    except RuntimeError:
        return False
    if supervised is None:
        return False
    if deadline is not None and gateway_deadline.now() >= deadline:
        return False
    try:
        process = psutil.Process(pid)
        running = process.is_running()
        parents = {p.pid for p in process.parents()}
        cwd = process.cwd()
        if deadline is not None and gateway_deadline.now() >= deadline:
            return False
        return (running and
                (supervised == pid or supervised in parents) and
                cwd == str(expected))
    except (psutil.Error, OSError):
        return False


def _launch_state(domain: str, label: str, *, runner=None, timeout: float = 5) -> str:
    result = (runner or subprocess.run)(["launchctl", "print", f"{domain}/{label}"],
                            capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if result.returncode == 0:
        pid = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
        last_exit = re.search(r"^\s*last exit (?:code|status)\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
        if (not pid or int(pid[1]) == 0) and last_exit and int(last_exit[1]) == 0:
            return "parked"
        return "loaded"
    if "Could not find service" in result.stderr or "Could not find service" in result.stdout:
        return "unloaded"
    raise RuntimeError(f"launchctl print could not establish unload (exit {result.returncode})")


# Health-proof slice inside rollback_switch, capped by its startup bound.
ROLLBACK_HEALTH_SECONDS = 12


def rollback_switch(home: Path, plist: Path, label: str, old: Path, *, domain: str | None = None,
                    launchctl_runner=None) -> bool:
    deadline = gateway_deadline.now() + STARTUP_SECONDS
    with deadline_scope(deadline):
        return _rollback_switch_bounded(home, plist, label, old, domain=domain,
                                        launchctl_runner=launchctl_runner, deadline=deadline)


@with_deadline_scope
def _rollback_switch_bounded(home: Path, plist: Path, label: str, old: Path, *, domain: str | None = None,
                            launchctl_runner=None, deadline: float = 0.0) -> bool:
    """Use S2 rollback with a targeted reload, never the ambient live gateway label."""
    from hermes_cli import gateway
    from hermes_cli.immutable_releases import wait_for_release_acknowledgement
    paths = ReleasePaths.for_home(home)
    pending = home / "release-txn.json"
    if pending.exists():
        from hermes_cli.immutable_releases import abandon_failed_switch
        abandon_failed_switch(home, candidate=paths.current.resolve(), previous=old)
        receipt(home, "abandon_switch", "recorded", candidate=str(paths.current.resolve()), previous=str(old))
    definition = plistlib.loads(plist.read_bytes())
    if label == gateway.get_launchd_label() and home.resolve() == get_hermes_home().resolve():
        body = gateway.generate_launchd_plist(release_target=old).encode("utf-8")
    else:
        # A disposable label uses its own plist; never regenerate the real service.
        definition["WorkingDirectory"] = str(old)
        body = plistlib.dumps(definition)
    reload_deadline = deadline
    domain = domain or _gateway_domain(label, None, runner=launchctl_runner,
                                       timeout=_remaining(reload_deadline, 10))
    def reload_target():
        import psutil
        from hermes_cli.gateway_launchd import _launchctl_bootstrap
        old_pid = _supervised_pid(label, runner=launchctl_runner,
                                  timeout=_remaining(reload_deadline, 10))
        (launchctl_runner or subprocess.run)(["launchctl", "bootout", f"{domain}/{label}"],
                                             capture_output=True,
                                             timeout=_remaining(reload_deadline, 15))
        if old_pid is not None:
            try:
                psutil.Process(old_pid).wait(timeout=_remaining(reload_deadline, 30))
            except psutil.NoSuchProcess:
                pass
        if launchctl_runner is None:
            _launchctl_bootstrap(domain, plist, label, timeout=_remaining(reload_deadline, 30))
        else:
            _launchctl_bootstrap(domain, plist, label, timeout=_remaining(reload_deadline, 30), runner=launchctl_runner)
        if gateway_deadline.now() >= reload_deadline:
            raise RuntimeError("guardian rollback deadline exceeded")
        return True
    result = rollback(home, plist_path=plist, plist_body=body, reload_callback=reload_target)
    if result.get("reload_pending"):
        # The S2 acknowledgement may arrive after this one-shot invocation; live
        # process identity below is the independent health proof for this action.
        wait_for_release_acknowledgement(home, timeout_seconds=_remaining(reload_deadline, 5))
    # The health phase is a slice of the rollback's single startup bound.
    health_deadline = min(reload_deadline, gateway_deadline.now() + ROLLBACK_HEALTH_SECONDS)
    while gateway_deadline.now() < health_deadline:
        if healthy(home, label, old, launchctl_runner, deadline=health_deadline):
            if gateway_deadline.now() < health_deadline:
                return True
            break
        if gateway_deadline.now() >= health_deadline:
            break
        time.sleep(min(.25, _remaining(health_deadline, .25)))
    return False


def _run(home: Path, plist: Path, label: str, *, grace: float, domain: str | None,
         forward_only: bool = False, launchctl_runner=None) -> str:
    deadline = gateway_deadline.now() + STARTUP_SECONDS
    with deadline_scope(deadline):
        return _run_bounded(home, plist, label, grace=grace, domain=domain,
                            forward_only=forward_only, launchctl_runner=launchctl_runner,
                            deadline=deadline)


@with_deadline_scope
def _run_bounded(home: Path, plist: Path, label: str, *, grace: float, domain: str | None,
                forward_only: bool = False, launchctl_runner=None, deadline: float = 0.0) -> str:
    from hermes_cli.immutable_releases import _verify_transaction
    if intent_path(home).exists():
        return "stopped"
    if not plist.is_file():
        receipt(home, "inspect", "alert", reason="gateway plist missing")
        return "alert"
    definition = plistlib.loads(plist.read_bytes())
    if (definition.get("Label") != label or
            Path(definition.get("EnvironmentVariables", {}).get("HERMES_HOME", "")).resolve() != home.resolve()):
        receipt(home, "inspect", "alert", reason="gateway plist identity mismatch")
        return "alert"
    if forward_only:
        from gateway.generation import GenerationCoordinator
        coordinator = GenerationCoordinator(home)
        if coordinator.service_label() != label:
            # One startup bound per guardian run: the repair inherits it, never a fresh one.
            repair_deadline = deadline
            cleanup_domain = _gateway_domain(label, domain, runner=launchctl_runner,
                                             timeout=_remaining(repair_deadline, 10))
            if _launch_state(cleanup_domain, label, runner=launchctl_runner,
                             timeout=_remaining(repair_deadline, 5)) == "parked":
                return _repair_parked(home, plist, label, cleanup_domain,
                                      Path(definition.get("WorkingDirectory", "")), launchctl_runner or subprocess.run,
                                      deadline=repair_deadline)
            return "waiting"
    paths = ReleasePaths.for_home(home)
    current = paths.current.resolve()
    if (not paths.current.is_symlink() or current.parent != paths.releases.resolve() or
            not _release_is_ready(current, current.name)):
        receipt(home, "inspect", "alert", reason="corrupt current pointer; no source fallback")
        return "alert"
    if Path(definition.get("WorkingDirectory", "")).resolve() != current:
        receipt(home, "inspect", "alert", reason="gateway plist is not rooted in current release")
        return "alert"
    switch_state, switch = _switch(home, grace=grace)
    if switch_state == "waiting":
        return "waiting"
    domain = _gateway_domain(label, domain, runner=launchctl_runner,
                             timeout=_remaining(deadline, 10))
    state = _launch_state(domain, label, runner=launchctl_runner,
                          timeout=_remaining(deadline, 5))
    launchctl = launchctl_runner or subprocess.run
    if state == "loaded" and healthy(home, label, current, launchctl_runner, deadline=deadline):
        pending = home / "release-txn.json"
        if pending.exists():
            record = json.loads(pending.read_text(encoding="utf-8-sig"))
            if record.get("operation") in {"rollback", "first-migration-rollback"}:
                from hermes_cli.immutable_releases import acknowledge_running_release
                acknowledge_running_release(home)
        return "healthy"
    if forward_only:
        if switch:
            # Parked repair is cold recovery. A pending update owns fresh A-prime.
            return "waiting"
        if state == "parked":
            return _repair_parked(home, plist, label, domain, current, launchctl,
                                  deadline=deadline)
        # A loaded process is never forced out. An unloaded service uses the
        # same coordinator fence and repair budget as a parked one.
        if state == "loaded":
            return "waiting"
        from gateway.generation import GenerationCoordinator
        if GenerationCoordinator(home).prepare_parked_repair(label, retire=False) != "repair":
            return "waiting"
    if switch:
        old = Path(switch["previous_intended"])
        if (old != paths.previous.resolve() or old == current or old.parent != paths.releases.resolve() or
                not _release_is_ready(old, old.name)):
            receipt(home, "rollback", "alert", reason="previous release is not intact", candidate=str(current))
            return "alert"
        if (home / "release-txn.json").exists():
            _verify_transaction(paths, switch)
        if _repair_count(home) >= MAX_REPAIRS:
            receipt(home, "rollback", "capped", candidate=str(current))
            return "capped"
        receipt(home, "rollback", "attempt", candidate=str(current), previous=str(old))
        ok = rollback_switch(home, plist, label, old, domain=domain, launchctl_runner=launchctl_runner)
        receipt(home, "rollback", "rolled_back" if ok else "failed", candidate=str(current), previous=str(old))
        return "rolled_back" if ok else "failed"
    if state in {"loaded", "parked"}:
        return "waiting"  # KeepAlive may be bringing up a registered job.
    if _repair_count(home) >= MAX_REPAIRS:
        receipt(home, "bootstrap", "capped", label=label)
        return "capped"
    receipt(home, "bootstrap", "attempt", label=label)
    if forward_only:
        from gateway.generation import GenerationCoordinator
        if GenerationCoordinator(home).prepare_parked_repair(label) != "repair":
            return "waiting"
        from hermes_cli.gateway_launchd_generation import refresh_generation_scope
        refresh_generation_scope(plist)
    launchctl(["launchctl", "bootstrap", domain, str(plist)], check=True,
              timeout=_remaining(deadline, 10))
    while gateway_deadline.now() < deadline:
        state = _launch_state(domain, label, runner=launchctl_runner,
                              timeout=_remaining(deadline, 5))
        if state in {"loaded", "parked"} and healthy(home, label, current, launchctl_runner, deadline=deadline):
            if gateway_deadline.now() < deadline:
                receipt(home, "bootstrap", "repaired", label=label, release=str(current))
                return "repaired"
            break
        if gateway_deadline.now() >= deadline:
            break
        _sleep = min(.25, _remaining(deadline, .25))
        time.sleep(_sleep)
    receipt(home, "bootstrap", "failed", label=label, reason="gateway not healthy after bootstrap")
    return "failed"


@with_deadline_scope
def _repair_parked(home, plist, label, domain, current, launchctl, *, deadline=None):
    from gateway.generation import GenerationCoordinator
    from hermes_cli.gateway_launchd_generation import refresh_generation_scope
    coordinator = GenerationCoordinator(home)
    action = coordinator.prepare_parked_repair(label, retire=False)
    if action == "waiting":
        receipt(home, "inspect", "waiting", label=label, reason="serving generation or unproven claimant")
        return "waiting"
    if action == "repair" and _repair_count(home) >= MAX_REPAIRS:
        receipt(home, "bootstrap", "capped", label=label)
        return "capped"
    if action == "repair":
        if coordinator.prepare_parked_repair(label) != "repair":
            return "waiting"
        receipt(home, "bootstrap", "attempt", label=label)
    own = gateway_deadline.now() + STARTUP_SECONDS
    enclosing = gateway_deadline.current()
    deadline = own if deadline is None else deadline
    if enclosing is not None:
        deadline = min(deadline, enclosing)
    launchctl(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True,
              timeout=_remaining(deadline, 10))
    if _launch_state(domain, label, runner=launchctl, timeout=_remaining(deadline, 5)) != "unloaded":
        raise RuntimeError("parked label bootout did not read back unloaded")
    if action == "cleanup":
        receipt(home, "bootout", "cleaned", label=label)
        return "cleaned"
    refresh_generation_scope(plist)
    launchctl(["launchctl", "bootstrap", domain, str(plist)], check=True,
              timeout=_remaining(deadline, 10))
    while gateway_deadline.now() < deadline:
        state = _launch_state(domain, label, runner=launchctl, timeout=_remaining(deadline, 5))
        if state == "loaded" and healthy(home, label, current, launchctl, deadline=deadline):
            if gateway_deadline.now() < deadline:
                receipt(home, "bootstrap", "repaired", label=label, release=str(current))
                return "repaired"
            break
        if gateway_deadline.now() >= deadline:
            break
        _sleep = min(.25, _remaining(deadline, .25))
        time.sleep(_sleep)
    receipt(home, "bootstrap", "failed", label=label, reason="gateway not healthy after bootstrap")
    return "failed"


def _repair_count(home: Path) -> int:
    cutoff = time.time() - 3600
    count = 0
    for path in (home / "logs/guardian").glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8-sig"))
            if row.get("action") in {"bootstrap", "rollback"} and row.get("outcome") == "attempt" and datetime.fromisoformat(row["at"]).timestamp() > cutoff:
                count += 1
        except (OSError, ValueError, KeyError):
            continue
    return count


def run_once(home: Path, plist: Path, label: str, *, grace: float | None = None,
             domain: str | None = None, launchctl_runner=None) -> str:
    import fcntl
    home = Path(home)
    directory = home / "logs/guardian"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "guardian.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        try:
            if intent_path(home).exists():
                return 'stopped'
            from hermes_cli.config import _validate_updates
            from hermes_cli.config_effective import load_user_config_effective
            config: dict[str, Any]
            if grace is None:
                config = load_user_config_effective(home / "config.yaml", fail_closed=True)
            else:
                config = {"updates": {"release_acknowledgement_timeout_seconds": grace}}
                # Explicit grace historically needs no config load. Only inspect the
                # opt-in marker when present; a malformed unrelated config cannot alert.
                config_path = home / "config.yaml"
                try:
                    flag_text = config_path.read_text(encoding="utf-8-sig") if config_path.is_file() else ""
                except (OSError, UnicodeError):
                    return "waiting"  # Unreadable flag cannot authorize legacy repair.
                if "overlap_handover" in flag_text or "forward_only_handover" in flag_text:
                    try:
                        flag_config = yaml.safe_load(flag_text) or {}
                    except yaml.YAMLError as exc:
                        receipt(home, "inspect", "alert", reason=str(exc))
                        return "alert"
                    if not isinstance(flag_config, dict):
                        receipt(home, "inspect", "alert", reason="overlap_handover config must be a mapping")
                        return "alert"
                    raw_gateway = flag_config.get("gateway") or {}
                    overlap = raw_gateway.get("overlap_handover") if isinstance(raw_gateway, dict) else None
                    if overlap is not None and (not isinstance(overlap, dict) or
                                                overlap.get("enabled") is not False):
                        return "waiting"
                    config["gateway"] = raw_gateway
            from gateway.generation import forward_only_handover_enabled
            forward_only = forward_only_handover_enabled(config)
            if forward_only and (home / 'forward-update.json').exists():
                from hermes_cli.gateway_forward_update import recover_forward, GenerationSupervisor
                proof = recover_forward(home, supervisor=GenerationSupervisor(home, runner=launchctl_runner,
                                        directory=Path(plist).parent, domain=domain))
                if proof is not None:
                    if proof['outcome'] == 'locked':
                        return 'locked'
                    receipt(home, 'forward_observe', proof['outcome'], generation=proof.get('new_id'),
                            reason=proof.get('failure', ''))
                    return 'healthy' if proof['outcome'] in {'success', 'rolled_back'} else 'waiting'
            if forward_only and (home / 'gateway-coordinator.db').exists():
                from hermes_cli.gateway_forward_update import cleanup_exited, GenerationSupervisor
                from gateway.generation import GenerationCoordinator
                supervisor = GenerationSupervisor(home, runner=launchctl_runner, directory=Path(plist).parent, domain=domain)
                cleanup_exited(home, supervisor=supervisor)
                service_label = GenerationCoordinator(home).service_label()
                # Canonical guardian installations follow the promoted service.
                # An explicit custom plist still addresses its requested label.
                if service_label != label and Path(plist).name == f'{label}.plist':
                    label = service_label
                    plist = Path(plist).with_name(f'{label}.plist')
            # The legacy guardian only knows one launchd label. Until overlap repair has
            # its own fenced protocol, it must not bootstrap or roll back either generation.
            gateway_config = config.get("gateway") or {}
            if (isinstance(gateway_config, dict) and
                    isinstance(gateway_config.get("overlap_handover"), dict) and
                    gateway_config["overlap_handover"].get("enabled") is True):
                return "waiting"
            if grace is not None:
                config = {"updates": {"release_acknowledgement_timeout_seconds": grace}}
            updates = config.get("updates")
            if updates is not None and not isinstance(updates, dict):
                raise ValueError("updates must be a mapping")
            scoped = {"updates": {"release_acknowledgement_timeout_seconds":
                                   (updates or {}).get("release_acknowledgement_timeout_seconds", 180.0)}}
            issues = []
            _validate_updates(scoped, issues)
            if issues:
                raise ValueError("; ".join(issue.message for issue in issues))
            if grace is None:
                grace = (config.get("updates") or {}).get(
                    "release_acknowledgement_timeout_seconds", 180.0)
            assert grace is not None
            return _run(home, Path(plist), label, grace=float(grace), domain=domain,
                        forward_only=forward_only, launchctl_runner=launchctl_runner)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, yaml.YAMLError) as exc:
            receipt(home, "inspect", "alert", reason=str(exc))
            return "alert"


def guardian_plist(home: Path, gateway_plist: Path, label: str, *, domain: str) -> bytes:
    python = home / "current/.venv/bin/python"
    return plistlib.dumps({"Label": GUARDIAN_LABEL, "RunAtLoad": True, "StartInterval": INTERVAL,
                           "ProgramArguments": [str(python), "-m", "hermes_cli.gateway_guardian", "run",
                                                "--gateway-plist", str(gateway_plist), "--gateway-label", label,
                                                "--domain", domain],
                           "WorkingDirectory": str(home / "current"),
                           "EnvironmentVariables": {"HERMES_HOME": str(home)},
                           "StandardOutPath": str(home / "logs/guardian/stdout.log"),
                           "StandardErrorPath": str(home / "logs/guardian/stderr.log")})


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent gateway guardian")
    parser.add_argument("action", choices=["install", "uninstall", "status", "run"])
    parser.add_argument("--gateway-plist", type=Path)
    parser.add_argument("--gateway-label")
    parser.add_argument("--domain", default=None)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("gateway guardian requires macOS launchd")
    from hermes_cli import gateway
    home = get_hermes_home()
    target = args.gateway_plist or gateway.get_launchd_plist_path()
    label = args.gateway_label or gateway.get_launchd_label()
    args.domain = args.domain or _domain(GUARDIAN_LABEL if args.action == "uninstall" else label)
    import pwd
    path = Path(pwd.getpwuid(getattr(os, 'getuid')()).pw_dir) / "Library/LaunchAgents" / f"{GUARDIAN_LABEL}.plist"
    if args.action == "status":
        print(f"enabled={enabled(home)} installed={path.is_file()} intent_stopped={intent_path(home).exists()}")
        from hermes_cli.gateway_generation_status import read_generation_status
        for row in read_generation_status(home):
            lease = ", ".join(row["leases"]) or "none"
            print(f"generation={row['id']} sha={row['release_sha']} label={row['label']} "
                  f"pid={row['pid']} lease={lease} state={row['state']}")
        return 0
    if args.action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"{args.domain}/{GUARDIAN_LABEL}"], capture_output=True, timeout=10)
        path.unlink(missing_ok=True)
        print("Guardian uninstalled")
        return 0
    if not enabled(home):
        parser.error("gateway.guardian.enabled must be true to install or run the guardian")
    if args.action == "run":
        outcome = run_once(home, target, label, domain=args.domain)
        print(outcome)
        return 0 if outcome in {"healthy", "stopped", "repaired", "rolled_back", "waiting", "locked", "cleaned"} else 1
    paths = ReleasePaths.for_home(home)
    if not paths.current.is_symlink() or not _release_is_ready(paths.current.resolve(), paths.current.resolve().name):
        parser.error("a valid immutable current release is required")
    if not (paths.current / ".venv/bin/python").is_file():
        parser.error("current release has no runnable Python interpreter")
    if not target.is_file():
        parser.error("install the gateway launchd plist before installing its guardian")
    definition = plistlib.loads(target.read_bytes())
    if (definition.get("Label") != label or
            Path(definition.get("EnvironmentVariables", {}).get("HERMES_HOME", "")).resolve() != home.resolve()):
        parser.error("gateway launchd plist does not match this label and home")
    body = guardian_plist(home, target, label, domain=args.domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    (home / "logs/guardian").mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == body:
        if _launch_state(args.domain, GUARDIAN_LABEL) == "unloaded":
            subprocess.run(["launchctl", "bootstrap", args.domain, str(path)], check=True, timeout=10)
        print(f"Guardian already installed: {path}")
        return 0
    if path.exists():
        parser.error("existing guardian plist differs; uninstall before reinstalling")
    path.write_bytes(body)
    subprocess.run(["launchctl", "bootstrap", args.domain, str(path)], check=True, timeout=10)
    print(f"Guardian installed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
