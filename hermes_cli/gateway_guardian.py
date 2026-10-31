"""One-shot macOS launchd guardian for an immutable Hermes gateway release.

The job is independent of the gateway process tree. It never updates source, retries
work, or falls back to the checkout when the release pointer is damaged.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import re
import subprocess
import sys
import time
import uuid
import hermes_yaml as yaml
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.forward_only_guard import _LAUNCHCTL_SERVICE_NOT_FOUND
from hermes_cli.immutable_releases import ReleasePaths, _release_is_ready, rollback

GUARDIAN_LABEL = "ai.hermes.gateway-guardian"
INTERVAL = 30
MAX_REPAIRS = 3
STARTUP_SECONDS = 45
ROLLBACK_SECONDS = 60
ROLLBACK_DRAIN_SECONDS = 15
POLL_SECONDS = 5


def _domain(label: str, *, deadline: float | None = None) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("gateway guardian requires macOS launchd")
    from hermes_cli.gateway_launchd import _probe_launchd_domain_for_label
    return _probe_launchd_domain_for_label(label, budget=lambda cap: _bounded_timeout(cap, deadline))


def _gateway_domain(label: str, preferred: str | None, *, deadline: float | None = None) -> str:
    """Observe both domains before trusting a saved domain or starting an unloaded job. Every
    launchctl probe here spends the caller's deadline, so discovery cannot run outside the bound."""
    domains = (f"gui/{os.getuid()}", f"user/{os.getuid()}")  # windows-footgun: ok (macOS launchd only)
    if preferred is not None and preferred not in domains:
        raise RuntimeError("guardian domain is not a gateway launchd domain for this user")
    states = {}
    for candidate in domains:
        try:
            states[candidate] = _launch_state(candidate, label, deadline=deadline)
        except subprocess.TimeoutExpired:
            states[candidate] = "unknown"
        except RuntimeError:
            if deadline is not None and time.monotonic() >= deadline:
                raise
            states[candidate] = "unknown"
    loaded = [candidate for candidate, state in states.items() if state in {"loaded", "parked"}]
    if len(loaded) > 1:
        raise RuntimeError("gateway label is loaded in both launchd domains")
    if loaded:
        return loaded[0]
    if "unknown" in states.values():
        raise RuntimeError("cannot prove gateway unloaded in both launchd domains")
    return preferred or _domain(label, deadline=deadline)


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
    config = load_user_config_effective(home / "config.yaml", fail_closed=True, validate_structure=False)
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


def healthy(home: Path, label: str, expected: Path, deadline: float | None = None) -> bool:
    """Running-release proof. ``deadline`` caps the ``launchctl list`` probe so a stalled launchctl
    cannot carry the guardian past its startup or rollback bound."""
    from gateway.status import read_runtime_status, runtime_status_is_stale
    from hermes_cli.gateway_launchd import _launchctl_supervised_pid
    import psutil
    state = read_runtime_status(home / "gateway_state.json") or {}
    pid = state.get("pid")
    if type(pid) is not int or state.get("gateway_state") not in {"running", "degraded"}:
        return False
    if state.get("code_sha") != expected.name or runtime_status_is_stale(state):
        return False
    if deadline is not None and deadline - time.monotonic() <= 0:
        return False
    supervised = _launchctl_supervised_pid(label, timeout=_bounded_timeout(10, deadline))
    if supervised is None:
        return False
    try:
        process = psutil.Process(pid)
        proven = (process.is_running() and
                  (supervised == pid or supervised in {p.pid for p in process.parents()}) and
                  process.cwd() == str(expected))
    except (psutil.Error, OSError):
        return False
    # A proof that finished after the bound does not count: the caller's deadline has already passed.
    return proven and (deadline is None or time.monotonic() < deadline)


def _bounded_timeout(requested: float, deadline: float | None) -> float:
    if deadline is None:
        return requested
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("guardian overall deadline expired")
    return min(requested, remaining)


def _sleep_within(seconds: float, deadline: float) -> None:
    """Poll pause that never sleeps past ``deadline``."""
    time.sleep(max(0.0, min(seconds, deadline - time.monotonic())))


def _launch_state(domain: str, label: str, *, deadline: float | None = None) -> str:
    result = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                            capture_output=True, text=True, encoding="utf-8", timeout=_bounded_timeout(5, deadline))
    if result.returncode == 0:
        # A loaded job with no live PID whose last exit was clean is parked: KeepAlive's
        # SuccessfulExit=false will never relaunch it (exit 0, or EX_CONFIG 78 mapped to 0).
        pid = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
        last_exit = re.search(r"^\s*last exit (?:code|status)\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
        if (not pid or int(pid[1]) == 0) and last_exit and int(last_exit[1]) == 0:
            return "parked"
        return "loaded"
    if _LAUNCHCTL_SERVICE_NOT_FOUND in result.stderr or _LAUNCHCTL_SERVICE_NOT_FOUND in result.stdout:
        return "unloaded"
    raise RuntimeError(f"launchctl print could not establish unload (exit {result.returncode})")


def _rollback_owners(home: Path, supervised: int | None):
    """Pin the gateway incarnation, not just launchd's short-lived wrapper."""
    import psutil
    from gateway.status import live_gateway_pid_for_home
    gateway = live_gateway_pid_for_home(home)
    owners = []
    if gateway is not None:
        try:
            process = psutil.Process(gateway)
            # Home identity alone cannot authorize killing a separately supervised process.
            if supervised is None:
                raise RuntimeError('cannot establish rollback gateway ownership: no supervised PID')
            if gateway != supervised and supervised not in {p.pid for p in process.parents()}:
                raise RuntimeError('rollback gateway is not owned by the selected launchd service')
            owners.append(process)
        except psutil.NoSuchProcess:
            gateway = None
    if supervised is not None and supervised != gateway:
        try:
            owners.append(psutil.Process(supervised))
        except psutil.NoSuchProcess:
            pass
    return gateway or (supervised if owners else None), owners


def _reap_rollback_owners(owners, deadline, *, home: Path | None = None):
    """Leave startup time inside the bound even if executor teardown wedges."""
    import psutil
    now = time.monotonic()
    # Reserve most of even a shortened rollback budget for bootstrap and health.
    drain_deadline = now + min(ROLLBACK_DRAIN_SECONDS, max(0.0, deadline - now) / 4)
    for process in owners:
        try:
            process.wait(timeout=max(0.0, drain_deadline - time.monotonic()))
        except psutil.TimeoutExpired:
            # bootout already sent SIGTERM. Pinned psutil handles reject PID reuse.
            try:
                from gateway.status import read_runtime_status
                state = (read_runtime_status(home / 'gateway_state.json') or {}) if home is not None else {}
                inflight = state.get('active_agents', 'unknown') if state.get('pid') == process.pid else 'unknown'
                logging.getLogger(__name__).warning(
                    'Rollback drain expired: killing pid=%s, last-reported in-flight agents=%s',
                    process.pid, inflight)
                process.kill()
                process.wait(timeout=_bounded_timeout(2, deadline))
            except psutil.NoSuchProcess:
                pass
        except psutil.NoSuchProcess:
            pass


def secret_preflight(home: Path, body: bytes) -> str | None:
    """Why a rendered gateway definition cannot load its configured secrets, else None.

    Checked before the running gateway is touched: a rollback plist whose PATH lacks
    the 1Password CLI starts a gateway with no platform tokens, which exits cleanly.
    """
    from agent.secret_sources.onepassword import find_op
    from hermes_cli.config_effective import load_user_config_effective
    config = load_user_config_effective(home / "config.yaml", fail_closed=True)
    onepassword = (config.get("secrets") or {}).get("onepassword") or {}
    if not isinstance(onepassword, dict) or onepassword.get("enabled") is not True:
        return None
    path = plistlib.loads(body).get("EnvironmentVariables", {}).get("PATH", "")
    if find_op(str(onepassword.get("binary_path") or ""), search_path=path) is None:
        return "rollback definition cannot resolve the 1Password op CLI on its PATH"
    return None


def rollback_switch(home: Path, plist: Path, label: str, old: Path, *, domain: str | None = None,
                    deadline: float | None = None) -> bool:
    """Use S2 rollback with a targeted reload, never the ambient live gateway label.

    When the rolled-back release is proven down after it was started (parked, unloaded or
    no supervised PID), restore the release that was serving, with the exact definition it
    ran under, rather than leave the gateway down. Only a serving release that was proven
    to run (acknowledged switch or health proof) is restored. Returns True only for a
    healthy rollback.
    """
    import psutil
    from hermes_cli import gateway
    from hermes_cli.immutable_releases import wait_for_release_acknowledgement
    deadline = deadline or time.monotonic() + ROLLBACK_SECONDS
    paths = ReleasePaths.for_home(home)
    running = paths.current.resolve()
    running_body = plist.read_bytes()
    serving_proven = _serving_proven(home, label, running, deadline)
    pending = home / "release-txn.json"
    if pending.exists():
        from hermes_cli.immutable_releases import abandon_failed_switch
        abandon_failed_switch(home, candidate=running, previous=old)
        receipt(home, "abandon_switch", "recorded", candidate=str(running), previous=str(old))
    definition = plistlib.loads(running_body)
    if label == gateway.get_launchd_label() and home.resolve() == get_hermes_home().resolve():
        body = gateway.generate_launchd_plist(release_target=old).encode("utf-8")
    else:
        # A disposable label uses its own plist; never regenerate the real service.
        definition["WorkingDirectory"] = str(old)
        body = plistlib.dumps(definition)
    if (reason := secret_preflight(home, body)) is not None:
        raise RuntimeError(reason)
    domain = domain or _domain(label, deadline=deadline)
    budget, touched = [deadline], []

    def reload_target():
        from hermes_cli.gateway_launchd import _launchctl_bootstrap, _launchctl_supervised_pid
        deadline = budget[0]
        old_pid = _launchctl_supervised_pid(label, timeout=_bounded_timeout(10, deadline), strict=True)
        target_pid, owners = _rollback_owners(home, old_pid)
        if target_pid is not None:
            # The rollback is a reload, not a crash: the gateway takes its bounded restart path.
            from gateway.status import write_planned_restart_marker
            if not write_planned_restart_marker(target_pid, hermes_home=home):
                raise RuntimeError('cannot mark the rollback gateway for planned restart')
        touched.append(True)
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True,
                       timeout=_bounded_timeout(ROLLBACK_SECONDS, deadline))
        _reap_rollback_owners(owners, deadline, home=home)
        # The exact remaining budget: flooring it to a whole second would hand the bootstrap a fresh
        # second past the rollback deadline.
        _launchctl_bootstrap(domain, plist, label, timeout=_bounded_timeout(ROLLBACK_SECONDS, deadline))
        return True
    try:
        result = rollback(home, plist_path=plist, plist_body=body, reload_callback=reload_target)
        if result.get("reload_pending"):
            # The S2 acknowledgement may arrive after this one-shot invocation; live
            # process identity below is the independent health proof for this action.
            wait_for_release_acknowledgement(home, timeout_seconds=_bounded_timeout(5, deadline))
        if _await_healthy(home, label, old, deadline):
            return True
    except (OSError, ValueError, RuntimeError, AttributeError, subprocess.SubprocessError, psutil.Error):
        if not touched:
            raise  # The serving gateway was never stopped: fail closed, as before.
        logging.getLogger(__name__).exception("Rollback to %s failed after the gateway stopped", old)
    if not serving_proven:
        # An expired, never-acknowledged switch: its release may never have started, so
        # reinstating it could only trade one down gateway for another.
        receipt(home, "restore", "skipped", release=str(running), reason="serving release never proven")
        return False
    if not _target_down(label, domain):
        # Still starting, not failed: give it its own startup window before judging it.
        if _await_healthy(home, label, old, time.monotonic() + STARTUP_SECONDS):
            return True
        if not _target_down(label, domain):
            receipt(home, "restore", "skipped", release=str(running), reason="rollback target still running")
            return False
    _restore_running(home, plist, label, running, running_body, budget, reload_target)
    return False


def _serving_proven(home: Path, label: str, running: Path, deadline: float) -> bool:
    """The release being rolled back from is known to have run: its switch was acknowledged by
    its own gateway, or it passes the running-release health proof now."""
    for name in ("release-txn.json", "release-last-txn.json"):
        try:
            record = json.loads((home / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        ack = record.get("reload_ack")
        if record.get("candidate") == str(running) and isinstance(ack, dict) and ack.get("at"):
            return True
    try:
        return healthy(home, label, running, deadline)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


def _target_down(label: str, domain: str) -> bool:
    """Proof the rollback target is not running: parked, unloaded, or no supervised PID.
    A probe that cannot answer is not proof."""
    from hermes_cli.gateway_launchd import _launchctl_supervised_pid
    try:
        if _launch_state(domain, label, deadline=time.monotonic() + 10) in {"parked", "unloaded"}:
            return True
        return _launchctl_supervised_pid(label, timeout=10, strict=True) is None
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


def _await_healthy(home: Path, label: str, release: Path, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if healthy(home, label, release, deadline):
            return True
        _sleep_within(.25, deadline)
    return False


def _restore_running(home: Path, plist: Path, label: str, running: Path, running_body: bytes,
                     budget: list[float], reload_target) -> None:
    """Re-activate the release that was serving before a rollback that did not come up.

    A ``restore`` activation: the admission canary does not watch it again, and a later
    ``hermes update --rollback`` still rolls back (it is not a completed rollback). The
    previous definition's bytes are the ones that release last started with.
    """
    import psutil
    from hermes_cli.immutable_releases import activate_release, wait_for_release_acknowledgement
    budget[0] = time.monotonic() + ROLLBACK_SECONDS
    receipt(home, "restore", "attempt", release=str(running))
    try:
        pending = home / "release-txn.json"
        if pending.exists():
            # The unacknowledged rollback is evidence, not work to finish: recovery would
            # reload the release that just failed to start.
            pending.rename(home / f"release-abandoned-{uuid.uuid4().hex}.json")
        result = activate_release(home, running, plist_path=plist, plist_body=running_body,
                                  reload_callback=reload_target, operation="restore", force_reload=True)
        if result.get("reload_pending"):
            wait_for_release_acknowledgement(home, timeout_seconds=_bounded_timeout(5, budget[0]))
        ok = _await_healthy(home, label, running, budget[0])
    except (OSError, ValueError, RuntimeError, AttributeError, subprocess.SubprocessError, psutil.Error) as exc:
        receipt(home, "restore", "failed", release=str(running), reason=str(exc))
        return
    receipt(home, "restore", "restored" if ok else "failed", release=str(running))


def _refuse_leftovers_before_launch(home: Path, deadline: float | None = None) -> None:
    """Never (re)bootstrap the legacy gateway beside withdrawn handover state.

    A live generation job plus a bootstrapped legacy job would be two pollers on one bot token.
    Raises RuntimeError (leftovers found or launchd uninspectable); run_once records it as an alert.
    Checked only right before a launch so inspection-only outcomes never shell out.
    """
    from hermes_cli.forward_only_guard import refuse_if_forward_only_leftovers
    refuse_if_forward_only_leftovers(home, timeout_for=lambda cap: _bounded_timeout(cap, deadline))


def _recover_switch(home: Path, plist: Path, label: str, current: Path,
                    paths: ReleasePaths, switch: dict, canary: dict | None,
                    domain: str, deadline: float) -> str:
    from hermes_cli import admission_canary
    from hermes_cli.immutable_releases import _verify_transaction
    old = Path(switch["previous_intended"])
    if (old != paths.previous.resolve() or old == current or old.parent != paths.releases.resolve() or
            not _release_is_ready(old, old.name)):
        receipt(home, "rollback", "alert", reason="previous release is not intact", candidate=str(current))
        if canary is not None:
            admission_canary.finish_rollback(home, canary, False)
        return "alert"
    if (home / "release-txn.json").exists():
        _verify_transaction(paths, switch)
    if _repair_count(home) >= MAX_REPAIRS:
        receipt(home, "rollback", "capped", candidate=str(current))
        if canary is not None:
            admission_canary.finish_rollback(home, canary, False)
        return "capped"
    _refuse_leftovers_before_launch(home, deadline)
    if canary is not None:
        canary = admission_canary.save(home, canary, outcome="rolling_back", previous=str(old))
    receipt(home, "rollback", "attempt", candidate=str(current), previous=str(old))
    try:
        ok = rollback_switch(home, plist, label, old, domain=domain, deadline=deadline)
    except Exception:
        if canary is not None:
            admission_canary.finish_rollback(home, canary, False)
        raise
    receipt(home, "rollback", "rolled_back" if ok else "failed", candidate=str(current), previous=str(old))
    if canary is not None:
        admission_canary.finish_rollback(home, canary, ok)
    return "rolled_back" if ok else "failed"


def _run(home: Path, plist: Path, label: str, *, grace: float, domain: str | None,
         deadline: float | None = None) -> str:
    deadline = deadline or time.monotonic() + STARTUP_SECONDS
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
    from hermes_cli import admission_canary
    canary_status, canary_switch, canary = admission_canary.inspect(home)
    canary_trip = canary_status in {"tripped", "rolling_back"}
    if canary_trip:
        switch = canary_switch
    domain = _gateway_domain(label, domain, deadline=deadline)
    state = _launch_state(domain, label, deadline=deadline)
    if not canary_trip and state == "loaded" and healthy(home, label, current, deadline):
        pending = home / "release-txn.json"
        if pending.exists():
            record = json.loads(pending.read_text(encoding="utf-8-sig"))
            if record.get("operation") in {"rollback", "restore", "first-migration-rollback"}:
                from hermes_cli.immutable_releases import acknowledge_running_release
                acknowledge_running_release(home, probe_timeout=_bounded_timeout(10, deadline))
        previous_canary = admission_canary.read_state(home)
        if previous_canary.get("outcome") == "rolling_back" and previous_canary.get("previous") == str(current):
            admission_canary.finish_rollback(home, previous_canary, True)
        return "healthy"
    if switch:
        return _recover_switch(home, plist, label, current, paths, switch,
                               canary if canary_trip else None, domain, deadline)
    if state == "loaded":
        return "waiting"  # KeepAlive may be bringing up a registered job.
    if _repair_count(home) >= MAX_REPAIRS:
        receipt(home, "bootstrap", "capped", label=label)
        return "capped"
    _refuse_leftovers_before_launch(home, deadline)
    receipt(home, "bootstrap", "attempt", label=label)
    if state == "parked":
        # Deliberate stops short-circuit above via the stopped intent, so a parked job here
        # is an unintended clean exit. Bootstrap would fail on a still-loaded label: boot it
        # out first and prove it unloaded. MAX_REPAIRS bounds a config-fatal relaunch loop.
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True,
                       timeout=_bounded_timeout(10, deadline))
        if _launch_state(domain, label, deadline=deadline) != "unloaded":
            receipt(home, "bootstrap", "failed", label=label, reason="parked label bootout did not read back unloaded")
            return "failed"
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True,
                   timeout=_bounded_timeout(10, deadline))
    deadline = deadline or time.monotonic() + 12
    while time.monotonic() < deadline:
        if _launch_state(domain, label, deadline=deadline) == "loaded" and healthy(home, label, current, deadline):
            receipt(home, "bootstrap", "repaired", label=label, release=str(current))
            return "repaired"
        _sleep_within(.25, deadline)
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


@contextmanager
def inspection_lock(home: Path):
    import fcntl
    directory = home / "logs/guardian"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "guardian.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def run_once(home: Path, plist: Path, label: str, *, grace: float | None = None,
             domain: str | None = None, deadline: float | None = None) -> str:
    home = Path(home)
    with inspection_lock(home) as acquired:
        if not acquired:
            return "locked"
        try:
            # Stopped intent is authoritative and must short-circuit config loading: a
            # deliberately parked gateway stays parked even if config is now malformed.
            if intent_path(home).exists():
                return "stopped"
            from hermes_cli.config import _validate_updates
            if grace is None:
                from hermes_cli.config_effective import load_user_config_effective
                config = load_user_config_effective(home / "config.yaml", fail_closed=True, validate_structure=False)
            else:
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
                        deadline=min(deadline, time.monotonic() + ROLLBACK_SECONDS) if deadline is not None else None)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, yaml.YAMLError) as exc:
            from hermes_cli import admission_canary
            canary = admission_canary.read_state(home)
            if canary.get("outcome") in {"tripped", "rolling_back"}:
                admission_canary.finish_rollback(home, canary, False)
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
        return 0 if outcome in {"healthy", "stopped", "repaired", "rolled_back", "waiting", "locked"} else 1
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
