"""macOS LaunchAgent fleet restart implementation for ``hermes update``."""

from __future__ import annotations


def restart_macos_launchd_gateways(
    restarted_services: list, failed_or_stale_units: list, drain_budget: float, *, require_supervision: bool = False,
    self_restart_pending: set | None = None, acknowledged_label: str | None = None,
) -> None:
    """Restart every launchd-managed gateway after an update (macOS)."""
    from hermes_cli import update_cmd_fleet as fleet
    from hermes_cli.gateway import (
        get_launchd_label, get_launchd_plist_path, launchd_gateway_labels_for_install, legacy_launchd_labels_for_install,
        _graceful_restart_via_sigusr1, _launchd_kickstart,
        _locate_launchd_gateway_service, _wait_for_launchd_service_pid,
    )
    if require_supervision:
        listing = fleet.subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            check=False)
        if listing.returncode != 0:
            failed_or_stale_units.append("launchd (listing failed)")
            return
    from hermes_cli.update_cmd_posix_pause import already_restarted, is_left_pending
    from hermes_cli.update_cmd_fleet_handover import restart_invoking_gateway
    current_label = get_launchd_label()
    resumed = already_restarted()["labels"]  # paused for the update, restarted on the new code
    protected_handover_labels = restart_invoking_gateway(
        restarted_services, failed_or_stale_units, current_label, resumed, acknowledged_label,
        self_restart_pending=self_restart_pending)

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
    from hermes_cli.update_fleet_scope import launchd_label_skip_notice
    for label in derived_labels + legacy_labels:
        if label == current_label or label in resumed or label in protected_handover_labels:
            continue
        # Labels are account-global: root B's default profile derives the same bare label root A
        # installed. A plist pinning a foreign HERMES_HOME, or launching another checkout, is another
        # install's job (#93349).
        if (notice := launchd_label_skip_notice(label)) is not None:
            print(notice)
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
            born = fleet._signalled_identity(old_pid) if old_pid else None
            if old_pid is not None and old_pid > 0 and not is_left_pending(old_pid):
                print(f"  → {label}: draining (up to {drain_budget:.0f}s)...")
                from hermes_cli.update_cmd_drain_report import drain_progress_reporter
                graceful_ok = _graceful_restart_via_sigusr1(
                    old_pid, drain_timeout=drain_budget,
                    on_progress=drain_progress_reporter(fleet._gateway_home_for_pid(old_pid), budget_s=drain_budget))
            if graceful_ok and _wait_for_launchd_service_pid(label, old_pid=old_pid, timeout=10.0, domain=domain):
                # KeepAlive already respawned it on new code — a kickstart would kill it.
                restarted_services.append(label)
                continue
            if not graceful_ok and old_pid and fleet._restart_left_pending(old_pid, label, self_restart_pending, born):
                continue
            try:
                _launchd_kickstart(label, domain)
            except fleet.subprocess.CalledProcessError as e:
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
        except fleet.subprocess.TimeoutExpired:
            failed_or_stale_units.append(label)
            print(f"  ⚠ launchctl timed out restarting {label}; continuing with remaining gateways")
