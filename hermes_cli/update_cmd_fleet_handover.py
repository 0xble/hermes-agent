"""Zero-cut handover routing for the update fleet."""

from __future__ import annotations


def restart_invoking_gateway(
    restarted_services: list,
    failed_or_stale_units: list,
    current_label: str,
    resumed: set,
    acknowledged_label: str | None,
    *,
    self_restart_pending: set | None,
) -> set[str]:
    """Apply planned handover accounting before the ordinary fleet restart path."""
    from hermes_cli.config import load_config_readonly
    from gateway.config import is_zero_cut_handover_enabled
    if is_zero_cut_handover_enabled(load_config_readonly()):
        from hermes_cli.gateway_launchd import get_launchd_slot_labels
        from hermes_cli.gateway_handover import planned_handover
        protected = set(get_launchd_slot_labels())
        if acknowledged_label in protected:
            restarted_services.append(acknowledged_label)
        elif planned_handover() is True:
            restarted_services.append(current_label)
        else:
            failed_or_stale_units.append(current_label)
            return protected | {current_label}
        if current_label in protected:
            return protected
    else:
        protected = set()

    if acknowledged_label == current_label:
        # The release transaction already booted this exact label and the new gateway acknowledged its loaded root.
        restarted_services.append(acknowledged_label)
    elif current_label not in resumed:
        from hermes_cli.update_cmd_fleet import _restart_launchd_gateway_after_update
        restarted, failed = _restart_launchd_gateway_after_update(
            supervision_verify=True, self_restart_pending=self_restart_pending)
        restarted_services.extend(restarted)
        failed_or_stale_units.extend(failed)
    return protected
