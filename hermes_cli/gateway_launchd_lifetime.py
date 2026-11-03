"""Opt-in launchd lifetime migration; never bootstrap before the old writers exit."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
from xml.parsers.expat import ExpatError

from hermes_cli.gateway_launchd_login import foreign_guardian, lifetime_lock
from hermes_cli.gateway_command_errors import LaunchdServiceError


SESSION_TYPES = {"user": "Background", "gui": "Aqua"}


def _uid() -> int:
    uid = getattr(os, "getuid", None)
    if uid is None:
        raise RuntimeError("launchd requires a POSIX user")
    return uid()


def configured_launchd_domain(label: str | None = None) -> str | None:
    from hermes_cli import gateway as gw
    path = gw.get_launchd_plist_path()
    if label is not None:
        path = path.with_name(f"{label}.plist")
    if not path.exists():
        # A successful user-domain install leaves an owned Aqua registration trigger.
        # Manual removal of the gateway plist must not silently revert it to automatic.
        from hermes_cli.gateway_launchd_login import _login_trigger_label
        trigger_label = _login_trigger_label(path.stem)
        trigger_path = path.with_name(f"{trigger_label}.plist")
        if trigger_path.exists():
            try:
                _owned_definition(trigger_path, gw.get_hermes_home(), trigger_label)
            except (OSError, RuntimeError, ValueError):
                return None  # An untrusted login trigger cannot select a service domain.
            return f"user/{_uid()}"
        return None
    try:
        body = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException, ExpatError):
        return None  # Existing refresh owns recovery of a malformed legacy definition.
    if not isinstance(body, dict):
        return None
    session = body.get("LimitLoadToSessionType")
    for mode, expected in SESSION_TYPES.items():
        if session == expected:
            return f"{mode}/{_uid()}"
    return None  # Historical Aqua/Background arrays retain automatic selection.


def _state(run, domain: str, label: str, *, timeout: float = 10) -> tuple[bool, int | None]:
    from hermes_cli import gateway as gw
    result = run(["launchctl", "print", f"{domain}/{label}"],
                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    if result.returncode == 0:
        return True, gw._parse_launchd_pid_from_print_output(result.stdout)
    if (result.returncode == 112 and domain.startswith("gui/")
            and "Could not find domain for user gui:" in result.stderr):
        return False, None
    if result.returncode not in {3, 113}:
        raise LaunchdServiceError(f"cannot inspect launchd target {domain}/{label}: {result.stderr}")
    return False, None


def _unload(run, domain: str, label: str, *, home: Path | None = None) -> None:
    from hermes_cli import gateway as gw
    loaded, pid = _state(run, domain, label)
    if not loaded:
        return
    owners = []
    if home is not None:
        from hermes_cli.gateway_guardian import _rollback_owners
        _, owners = _rollback_owners(home, pid)
    result = run(["launchctl", "bootout", f"{domain}/{label}"],
                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False)
    if result.returncode not in {0, 3, 113}:
        raise RuntimeError(f"cannot unload {domain}/{label}: {result.stderr}")
    if pid and not gw._wait_for_pid_exit(pid, 65):
        raise RuntimeError(f"{domain}/{label} PID {pid} has not exited; refusing a second writer")
    import psutil
    for owner in owners:
        try:
            owner.wait(timeout=65)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired as exc:
            raise RuntimeError(f"gateway writer PID {owner.pid} still draining; refusing replacement") from exc
    if _state(run, domain, label)[0]:
        raise RuntimeError(f"{domain}/{label} remains loaded; refusing a second writer")


def _retire_copies(run, home, domains, names, *, gateway_label):
    for name in names:
        for domain in domains:
            _unload(run, domain, name, home=home if name == gateway_label else None)


def _write(path: Path, body: bytes | None) -> None:
    if body is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".plist.pending")
    pending.write_bytes(body)
    pending.replace(path)


def _owned_definition(path: Path, home: Path, label: str) -> bytes | None:
    if not path.exists():
        return None
    try:
        original = path.read_bytes()
        body = plistlib.loads(original)
    except (OSError, ValueError, IndexError, plistlib.InvalidFileException, ExpatError) as exc:
        raise LaunchdServiceError(f"cannot read launchd definition {path}: {exc}") from exc
    env = body.get("EnvironmentVariables") if isinstance(body, dict) else None
    owner = env.get("HERMES_HOME") if isinstance(env, dict) else None
    if (not isinstance(body, dict) or body.get("Label") != label
            or not isinstance(owner, str) or not owner or Path(owner).resolve() != home.resolve()):
        raise LaunchdServiceError(f"refusing foreign or malformed launchd definition: {path}")
    return original


def _inspect_layout(run, domains, labels, originals, *, mode, gateway_label):
    from hermes_cli import gateway as gw
    states = {(domain, name): _state(run, domain, name)
              for domain in domains for name in labels.values()}
    if mode == "gui":
        available = run(["launchctl", "print", domains[0]], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if available.returncode != 0:
            raise RuntimeError("GUI rollback requires an existing Aqua login domain")
    for path, name in labels.items():
        if originals[path] is None and any(states[(domain, name)][0] for domain in domains):
            raise RuntimeError(f"cannot prove ownership of loaded label {name} without its definition")
    for domain in domains:
        listing = run(["launchctl", "print", domain], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        gui_absent = (listing.returncode == 112 and domain.startswith("gui/")
                      and "Could not find domain for user gui:" in listing.stderr)
        if listing.returncode != 0 and not gui_absent:
            raise RuntimeError(f"cannot inspect pending helpers in {domain}: {listing.stderr}")
        if f"{gateway_label}.reload." in listing.stdout:
            raise RuntimeError("pending deferred reload helper; let it finish before lifetime migration")
    extras = set(gw.launchd_gateway_labels_for_install()) | set(gw.legacy_launchd_labels_for_install())
    # The guardian shares the gateway glob but is not a legacy traffic writer.
    # Its owning home is handled (or explicitly skipped) in labels above.
    from hermes_cli.gateway_guardian import GUARDIAN_LABEL
    for extra in extras - set(labels.values()) - {GUARDIAN_LABEL}:
        if any(_state(run, domain, extra)[0] for domain in domains):
            raise RuntimeError(f"stop and remove legacy/standby label {extra} before domain migration")
    if any(sum(states[(domain, name)][0] for domain in domains) > 1 for name in labels.values()):
        raise RuntimeError("label is loaded in both domains; stop both copies before migration")
    return states


def _restore_definitions(run, originals, labels, states, *, gateway_label, stopped) -> bool:
    for destination, data in originals.items():
        _write(destination, data)
    for (domain, name), (loaded, pid) in states.items():
        source = next(path for path, own_label in labels.items() if own_label == name)
        authorized = name != gateway_label or (not stopped and pid is not None)
        if loaded and originals[source] is not None and authorized:
            run(["launchctl", "bootstrap", domain, str(source)], check=True, timeout=30)
    return stopped or not any(name == gateway_label and pid is not None
                              for (_, name), (_, pid) in states.items())


def _retirement_failure(run, states, gateway_label: str, original_gateway: bytes | None) -> str:
    """Report partial retirement without reloading beside a possibly undrained writer."""
    unloaded, loaded, unknown = [], [], []
    for (domain, name), (was_loaded, _) in states.items():
        if not was_loaded:
            continue
        target = f"{domain}/{name}"
        try:
            (loaded if _state(run, domain, name)[0] else unloaded).append(target)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            unknown.append(target)
    # Loaded placement is not durable intent (the gateway may be stopped or misplaced).
    original = plistlib.loads(original_gateway) if original_gateway is not None else {}
    prior = next((mode for mode, session in SESSION_TYPES.items()
                  if original.get("LimitLoadToSessionType") == session), None)
    configured = configured_launchd_domain(gateway_label)
    if prior is None and configured is not None:
        prior = configured.split("/", 1)[0]
    repair = (f"`hermes gateway install --launchd-domain {prior} --no-start-now` "
              "and `hermes gateway start`." if prior else
              "select the intended --launchd-domain from the original definition before reinstalling.")
    return (f"Unloaded: {', '.join(unloaded) or 'none'}. "
            f"Still loaded: {', '.join(loaded) or 'none'}. "
            f"Unknown: {', '.join(unknown) or 'none'}. "
            "Stopped intent retained; any unregistered writer may still be draining. "
            "Repair: verify all writers have exited with `hermes gateway stop` and "
            "`hermes gateway status --deep`, then run " + repair)


def _running_writer(home: Path) -> bool:
    from gateway.status import get_running_pid, live_gateway_pid_for_home
    return (get_running_pid(cleanup_stale=False) is not None
            or live_gateway_pid_for_home(home) is not None)


def migrate_launchd_domain(mode: str, *, start_now: bool, runner=None, force_refresh: bool = False) -> None:
    import fcntl  # POSIX-only path; importing gateway CLI remains safe on Windows.
    if mode not in SESSION_TYPES:
        raise ValueError("launchd domain must be 'gui' or 'user'")
    from hermes_cli import gateway as gw
    from hermes_cli.gateway_guardian import GUARDIAN_LABEL, guardian_plist, set_intent
    home, path, label = gw.get_hermes_home(), gw.get_launchd_plist_path(), gw.get_launchd_label()
    guardian_path = path.with_name(f"{GUARDIAN_LABEL}.plist")
    from hermes_cli.gateway_launchd_login import _login_trigger_label

    login_label = _login_trigger_label(label)
    login_path = path.with_name(f"{login_label}.plist")
    run = runner or subprocess.run
    domains = (f"gui/{_uid()}", f"user/{_uid()}")
    target = f"{mode}/{_uid()}"
    labels = {login_path: login_label, path: label}
    if not foreign_guardian(guardian_path, home, GUARDIAN_LABEL):
        labels[guardian_path] = GUARDIAN_LABEL
    originals = {p: _owned_definition(p, home, name) for p, name in labels.items()}
    # Validate/render before stopping anything. A foreign guardian must never be adopted.
    from hermes_cli.immutable_releases import resolved_release
    release = resolved_release(home)
    body = (gw.generate_launchd_plist(release_target=release, domain=mode) if release is not None
            else gw.generate_launchd_plist(domain=mode)).encode()
    from hermes_cli.gateway_service_owner import refuse_temp_home_service_write
    if refuse_temp_home_service_write(body.decode(), "launchd lifetime migration"):
        raise RuntimeError("refusing temporary-home service install")
    from hermes_cli.gateway_launchd_login import login_plist
    bodies = {path: body, login_path: login_plist(home, path, label) if mode == "user" else None}
    if originals.get(guardian_path) is not None:
        bodies[guardian_path] = guardian_plist(home, path, label, domain=target, explicit=True)
    with lifetime_lock(home, blocking=False) as acquired:
        if not acquired:
            raise BlockingIOError("launchd lifetime operation is busy; retry shortly")
        if any((p.read_bytes() if p.exists() else None) != b for p, b in originals.items()):
            raise RuntimeError("launchd definitions changed during migration preflight; retry")
        states = _inspect_layout(run, domains, labels, originals, mode=mode, gateway_label=label)
        if force_refresh and (_running_writer(home) or any(
                name == label and pid is not None for (_, name), (_, pid) in states.items())):
            raise LaunchdServiceError(
                "stop the gateway (hermes gateway stop) before refreshing its explicit launchd definition")
        correct = all(originals[p] == b for p, b in bodies.items())
        placed = all(not loaded or domain == (domains[0] if name == login_label else target)
                     for (domain, name), (loaded, _) in states.items())
        guardian_placed = guardian_path not in bodies or states[(target, GUARDIAN_LABEL)][0]
        if not force_refresh and correct and placed and guardian_placed and (not start_now or states[(target, label)][0]):
            if not start_now and states[(target, label)][0]:
                raise RuntimeError("stop the gateway before installing with --no-start-now")
            if not start_now:
                set_intent(home, stopped=True)
            gw._clear_launchd_unsupported_marker()
            print(f"Service already installed in {target}")
            return
        if _running_writer(home):
            raise RuntimeError("stop the gateway before changing its launchd domain")
        from hermes_cli.gateway_launchd_login import stop_requested
        stopped = stop_requested(home)
        set_intent(home, stopped=True)
        names = [name for name in (login_label, GUARDIAN_LABEL, label) if name in labels.values()]
        changed = False
        try:
            # Freeze repair before retiring either gateway definition.
            try:
                _retire_copies(run, home, domains, names, gateway_label=label)
            except Exception as exc:
                raise RuntimeError(f"{exc}. {_retirement_failure(run, states, label, originals[path])}") from exc
            changed = True
            from gateway.status import live_gateway_pid_for_home
            if live_gateway_pid_for_home(home) is not None:
                raise RuntimeError("gateway writer still alive after all label drains; refusing replacement")
            gw._prepare_service_launcher()
            for destination, data in bodies.items():
                _write(destination, data)
            if start_now:
                set_intent(home, stopped=False)
                run(["launchctl", "bootstrap", target, str(path)], check=True, timeout=30)
                if not _state(run, target, label)[0]:
                    raise RuntimeError("bootstrap returned without registering the gateway")
            if guardian_path in bodies:
                run(["launchctl", "bootstrap", target, str(guardian_path)], check=True, timeout=30)
                if not _state(run, target, GUARDIAN_LABEL)[0]:
                    raise RuntimeError("bootstrap returned without registering the guardian")
        except Exception:
            # Unknown drain state must remain stopped; never restore a second copy beside it.
            set_intent(home, stopped=True)
            if changed:
                _retire_copies(run, home, domains, names, gateway_label=label)
                from gateway.status import live_gateway_pid_for_home
                if live_gateway_pid_for_home(home) is not None:
                    raise RuntimeError("replacement writer remains alive; rollback stays stopped")
                stopped = _restore_definitions(run, originals, labels, states,
                                               gateway_label=label, stopped=stopped)
                set_intent(home, stopped=stopped)
            raise
        gw._clear_launchd_unsupported_marker()
        gw._resolved_launchd_domain = target
    print(f"Service installed in {target}; gateway {'loaded' if start_now else 'stopped'}")


def uninstall_launchd_lifetime(*, runner=None) -> None:
    from hermes_cli import gateway as gw
    from hermes_cli.gateway_guardian import GUARDIAN_LABEL, set_intent
    run = runner or subprocess.run
    home, path = gw.get_hermes_home(), gw.get_launchd_plist_path()
    label = gw.get_launchd_label()
    from hermes_cli.gateway_launchd_login import _login_trigger_label

    labels = [_login_trigger_label(label), label]
    with lifetime_lock(home, announce_wait=True):
        if not foreign_guardian(path.with_name(f"{GUARDIAN_LABEL}.plist"), home, GUARDIAN_LABEL):
            labels.insert(1, GUARDIAN_LABEL)
        for name in labels:
            original = _owned_definition(path.with_name(f"{name}.plist"), home, name)
            if original is None and any(_state(run, domain, name)[0]
                                        for domain in (f"gui/{_uid()}", f"user/{_uid()}")):
                raise RuntimeError(f"cannot prove ownership of loaded label {name} without its definition")
        set_intent(home, stopped=True)
        for name in labels:
            for domain in (f"gui/{_uid()}", f"user/{_uid()}"):
                _unload(run, domain, name, home=home if name == label else None)
            if name == label:
                from gateway.status import live_gateway_pid_for_home
                if live_gateway_pid_for_home(home) is not None:
                    raise RuntimeError("gateway writer still alive; uninstall remains stopped and retains definition")
            path.with_name(f"{name}.plist").unlink(missing_ok=True)
    gw._resolved_launchd_domain = None
    print("Gateway uninstalled" + (" with its guardian" if GUARDIAN_LABEL in labels else "; shared guardian preserved"))
