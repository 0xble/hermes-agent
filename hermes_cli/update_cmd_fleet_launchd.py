"""Launchd gateway restart and release acknowledgement for the update fleet."""

from __future__ import annotations

from pathlib import Path


def _acknowledged_release_launchd_label(home: Path, root: Path) -> str | None:
    """Only the transaction's acknowledged, installed plist can exempt its own label."""
    from hermes_cli.update_cmd_fleet import Path, _FLEET_PROBE_SETTLE_TIMEOUT_SECONDS, _time, json
    import hashlib
    import plistlib
    import psutil
    from hermes_cli import gateway

    try:
        if (home / "release-txn.json").exists():
            return None
        record = json.loads((home / "release-last-txn.json").read_text(encoding="utf-8-sig"))
        ack, plist = record["reload_ack"], record["plist"]
        if not record["requires_reload"] or Path(ack["release_root"]).resolve() != root.resolve():
            return None
        installed = gateway.get_launchd_plist_path()
        if Path(plist["path"]).resolve() != installed.resolve():
            return None
        body = installed.read_bytes()
        label = gateway.get_launchd_label()
        definition = plistlib.loads(body)
        if (hashlib.sha256(body).hexdigest() != plist["intended_sha256"]
                or ack["plist_sha256"] != plist["intended_sha256"]
                or definition["Label"] != label
                or Path(definition["EnvironmentVariables"]["HERMES_HOME"]).resolve() != home.resolve()):
            return None
        # A durable ACK is historical. Credit it only while the very gateway it
        # acknowledged is still in this label's live supervised process tree.
        from hermes_cli.gateway_launchd import _launchctl_supervised_pid
        supervisor_pid = _launchctl_supervised_pid(label)
        if not supervisor_pid or supervisor_pid != ack["launchd_pid"]:
            return None
        supervisor = psutil.Process(supervisor_pid)
        gateway_pid = ack["gateway_pid"]
        processes = [supervisor, *supervisor.children(recursive=True)]
        if not any(p.pid == gateway_pid and Path(p.cwd()).resolve() == root.resolve()
                   for p in processes):
            return None
        from hermes_cli.update_receipt import collect_fleet_versions
        resolved_root = root.resolve()

        def row_matches_release(row: dict) -> bool:
            code_root = row.get("code_root")
            if not isinstance(code_root, str):
                return False
            try:
                return row.get("state") == "current" and Path(code_root).resolve() == resolved_root
            except (OSError, TypeError, ValueError):
                return False

        def row_for_gateway(rows: list[dict]) -> dict | None:
            return next((row for row in rows if row.get("pid") == gateway_pid), None)

        # A gateway publishes its fleet row after startup. A row for this PID is
        # authoritative when present: stale or mismatched identity must take the
        # normal one-time corrective relaunch. Only the absence of this PID is
        # transient, so wait within the same settle bound used by post-update fleet
        # verification before crediting the ACK without a row.
        rows = collect_fleet_versions(
            expected_sha_override=root.name, expected_root_override=resolved_root)
        row = row_for_gateway(rows)
        if row is not None:
            return label if row_matches_release(row) else None
        deadline = _time.monotonic() + _FLEET_PROBE_SETTLE_TIMEOUT_SECONDS
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            _time.sleep(min(2.0, remaining))
            row = row_for_gateway(collect_fleet_versions(
                expected_sha_override=root.name, expected_root_override=resolved_root))
            if row is not None:
                return label if row_matches_release(row) else None
        # Verification below remains the authority if the row never appears.
        return label
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None


def _restart_launchd_gateway_after_update(
    *, supervision_verify: bool = True, self_restart_pending: set | None = None,
) -> tuple[list, list]:
    """Restart the invoking profile's launchd gateway after an update.

    No ``launchctl list`` gating: a booted-out job (plist present, definition
    deregistered) fails it, and it can exit non-zero while the job is alive — gating on it
    silently skipped the restart yet printed "Update complete!". When the plist exists
    ``launchd_restart()`` always runs; every failure path is loud with a manual recovery
    command. Returns ``(restarted_labels, failed_labels)``; with ``supervision_verify``
    success also requires a fresh supervised PID ("the call returned" is not "supervised").

    74973 (salvage #75021 by @jeff-mettel): the restart used to be gated on ``launchctl list <label>``
    exiting 0. A *booted-out* job — plist present, definition deregistered from launchd (crashed helper,
    manual bootout, failed prior update) — fails that check, so the whole branch silently skipped: no
    restart, no message, ``KeepAlive`` unable to revive a definition launchd no longer knows, and the update
    still printed "Update complete!".
    See #88848.
    """
    from hermes_cli.update_cmd_fleet import subprocess
    from hermes_cli.gateway import (
        get_launchd_label, get_launchd_plist_path, launchd_restart, wait_for_launchd_gateway_supervision,
        _is_pid_ancestor_of_current_process, _launchctl_supervised_pid, _ancestor_chain_to,
    )
    current_label = get_launchd_label()
    old_pid = None
    try:
        if not get_launchd_plist_path().exists():
            return [], []  # not a launchd install — nothing to do or warn
        # Snapshot BEFORE the restart: "supervising some pid" was true before too, so only a pid that
        # actually changed distinguishes a restart from a no-op (the sibling loop's contract). Read-only
        # and verification-only — the restart itself is never gated on `launchctl list` (#74973).
        old_pid = _launchctl_supervised_pid(current_label) if supervision_verify else None
        try:
            launchd_restart()
        except subprocess.CalledProcessError as e:
            stderr = (getattr(e, "stderr", "") or "").strip()
            print(
                f"  ⚠ Gateway restart failed: {stderr}\n"
                "    The gateway may be DOWN on pre-update code. "
                "Recover manually: hermes gateway restart"
            )
            return [], [current_label]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # A plist exists, so a gateway is SUPPOSED to be supervised; a broken/wedged
        # launchctl is not proof nothing needs restarting. Count it, tell the operator.
        print(
            # The old code `pass`ed here (#74973's second silent variant); count it and tell the operator.
            "  ⚠ Could not restart the gateway "
            f"({e.__class__.__name__}: {e}).\n"
            "    Recover manually: hermes gateway restart"
        )
        return [], [current_label]

    if not supervision_verify:
        return [current_label], []
    if old_pid is not None and _is_pid_ancestor_of_current_process(old_pid):
        # launchd_restart() handed the restart to the gateway this updater runs INSIDE (cron job in
        # the gateway tree, #100179): it exits only after this process does, so no fresh supervised
        # pid can appear while we wait. Record it as pending for the fleet matrix (#119597) — together
        # with every process between it and this updater: launchd supervises a wrapper (osascript →
        # stderr_timestamp → gateway), and the gateway row carries the gateway's own pid.
        if self_restart_pending is not None:
            self_restart_pending.add(old_pid)
            self_restart_pending.update(_ancestor_chain_to(old_pid))
        return [current_label], []

    # launchd_restart() returning only means "restart REQUESTED" (async). A helper dying
    # before first bootstrap, or a bootstrap exiting 0 without registering (macOS 26.6.1),
    # would otherwise reach "Update complete!" unsupervised. Verified domain-agnostically:
    # domain locate fails on macOS-26 per-user domains.
    # launchd_restart() returning is only "restart REQUESTED" — the self-restart branch hands work to the
    # running gateway, a plist reload to a detached helper; both asynchronous. See #88848.
    if wait_for_launchd_gateway_supervision(label=current_label, old_pid=old_pid):
        return [current_label], []
    print(
        f"  ✗ {current_label} restarted but launchd is not supervising a new process for it.\n"
        "    Check logs, then: hermes gateway restart"
    )
    return [], [current_label]


def _restart_macos_launchd_gateways(
    restarted_services: list, failed_or_stale_units: list, drain_budget: float, *, require_supervision: bool = False,
    self_restart_pending: set | None = None, acknowledged_label: str | None = None,
) -> None:
    """Restart every launchd-managed gateway after an update (macOS).

    The pull is shared across profiles, so every ``ai.hermes.gateway*`` LaunchAgent
    must reload it or siblings stay on pre-update ``sys.modules`` (systemd parity).
    Invoking profile uses ``launchd_restart()``; siblings get the same drain-first
    sequence with their domain (``gui/<uid>`` vs ``user/<uid>``) resolved per label so
    none is kickstarted in the wrong domain. ``TimeoutExpired`` is isolated per label.

    See #41403.
    The invoking profile keeps the existing ``launchd_restart()`` treatment (self-restart request → graceful
    drain → kickstart). ``subprocess.TimeoutExpired`` is isolated per label so one wedged launchctl call
    cannot leave the rest of the fleet on old code (#68523).
    """
    from hermes_cli.update_cmd_fleet import (
        _gateway_home_for_pid, _restart_launchd_gateway_after_update, subprocess,
    )
    from hermes_cli.gateway import (
        get_launchd_label, get_launchd_plist_path, launchd_gateway_labels_for_install,
        legacy_launchd_labels_for_install, _graceful_restart_via_sigusr1, _launchd_kickstart,
        _locate_launchd_gateway_service, _wait_for_launchd_service_pid,
    )
    if require_supervision:
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if listing.returncode != 0:
            failed_or_stale_units.append("launchd (listing failed)")
            return
    from hermes_cli.update_cmd_posix_pause import already_restarted
    current_label = get_launchd_label()
    resumed = already_restarted()["labels"]  # paused for the update, restarted on the new code
    if acknowledged_label == current_label:
        # The release transaction already booted this exact label and the new
        # gateway acknowledged its loaded root. Fleet verification still checks
        # its successor; only the second mutating relaunch is omitted.
        restarted_services.append(acknowledged_label)
    elif current_label not in resumed:
        _restarted, _failed = _restart_launchd_gateway_after_update(
            supervision_verify=True, self_restart_pending=self_restart_pending)
        restarted_services.extend(_restarted)
        failed_or_stale_units.extend(_failed)

    derived_labels = launchd_gateway_labels_for_install()
    # Units labelled before the profile-name suffix scheme (ai.hermes.gateway-<hash>) are invisible
    # to the derivation; legacy_launchd_labels_for_install() credits one only when its plist is
    # provably this install's, so the #41403 boundary (never touch another install's fleet) holds.
    # See #115254.
    legacy_labels = legacy_launchd_labels_for_install(exclude=set(derived_labels) | {current_label})
    # The opt-in guardian has a gateway-looking label but is not a gateway fleet
    # member. When disabled, its stale plist must not be kickstarted (it exits 2
    # and only adds a second launchd action to the update). Enabled guardians keep
    # their existing restart behavior.
    if "ai.hermes.gateway-guardian" in legacy_labels:
        from hermes_cli.gateway_guardian import enabled as guardian_enabled
        from hermes_constants import get_hermes_home
        if not guardian_enabled(get_hermes_home()):
            legacy_labels.remove("ai.hermes.gateway-guardian")
    if legacy_labels:
        print(f"  ↻ legacy-labelled units of this install join the restart: {', '.join(legacy_labels)}")
    from hermes_cli.update_fleet_scope import describe_skipped_runtime, launchd_label_foreign_home
    for label in derived_labels + legacy_labels:
        if label == current_label or label in resumed:
            continue
        # Labels are account-global: root B's default profile derives the same bare label root A
        # installed. A plist pinning a foreign HERMES_HOME is another install's job (#93349).
        if (foreign_home := launchd_label_foreign_home(label)) is not None:
            print(describe_skipped_runtime("launchd job", label, foreign_home))
            continue
        try:
            # Locate = liveness + domain in one probe; kickstart and fresh-PID checks
            # reuse that domain so a sibling is never probed in one and restarted in another.
            domain, old_pid = _locate_launchd_gateway_service(label)
            if domain is None:
                if require_supervision and get_launchd_plist_path().with_name(f"{label}.plist").exists():
                    failed_or_stale_units.append(label)
                continue  # A profile without an installed job has no restart target.
            graceful_ok = False
            if old_pid is not None and old_pid > 0:
                print(f"  → {label}: draining (up to {drain_budget:.0f}s)...")
                from hermes_cli.update_cmd_drain_report import drain_progress_reporter
                graceful_ok = _graceful_restart_via_sigusr1(
                    old_pid, drain_timeout=drain_budget,
                    on_progress=drain_progress_reporter(_gateway_home_for_pid(old_pid), budget_s=drain_budget))
            if graceful_ok and _wait_for_launchd_service_pid(label, old_pid=old_pid, timeout=10.0, domain=domain):
                # KeepAlive already respawned it on new code — a kickstart would kill it.
                restarted_services.append(label)
                continue
            try:
                _launchd_kickstart(label, domain)
            except subprocess.CalledProcessError as e:
                stderr = (getattr(e, "stderr", "") or "").strip()
                failed_or_stale_units.append(label)
                print(
                    f"  ⚠ Failed to restart {label}: {stderr}\n"
                    f"    Recover manually: launchctl kickstart -k {domain}/{label}"
                )
                continue
            if _wait_for_launchd_service_pid(label, old_pid=old_pid, timeout=15.0, domain=domain):
                restarted_services.append(label)
            else:
                failed_or_stale_units.append(label)
                print(
                    f"  ✗ {label} failed to come back after restart.\n"
                    f"    Check logs, then: launchctl kickstart -k {domain}/{label}"
                )
        except subprocess.TimeoutExpired:
            failed_or_stale_units.append(label)
            print(f"  ⚠ launchctl timed out restarting {label}; continuing with remaining gateways")
