"""Manual fleet restart selection phases kept outside the fleet orchestrator."""

import logging

logger = logging.getLogger(__name__)


def new_supervisor_gateway_pids(candidate_pids, mapped_processes, pre_restart_pids):
    """Protect only external-supervisor children born after the restart snapshot."""
    from hermes_cli.gateway import _capture_gateway_argv
    from hermes_cli.gateway_supervised_restart import gateway_declares_external_supervisor

    # Only a supervisor child that appeared after the restart snapshot is outside
    # this sweep. A pre-existing externally supervised gateway still has to leave
    # its old process so the supervisor can relaunch it on the new modules (#88654).
    pre_restart_pids = set(pre_restart_pids) if pre_restart_pids is not None else None
    externally_supervised_pids = set()
    for pid in candidate_pids:
        proc = mapped_processes.get(pid)
        if proc is not None:
            # A mapped process gets the control-socket/state-file check as well as
            # the live argv fallback, scoped to that profile's home.
            try:
                declares_external_supervisor = gateway_declares_external_supervisor(pid, proc.path)
            except Exception as exc:
                logger.debug("Could not read supervisor identity for PID %s", pid, exc_info=True)
                # An unreadable identity is ambiguous; never turn it into a
                # destructive manual-stop decision. Report the skipped process so
                # the update summary/receipt is not silently incomplete.
                print(
                    f"  ⚠ Could not determine external-supervisor ownership for "
                    f"gateway PID {pid} ({proc.profile}): {exc}; "
                    "protecting it if it appeared after the restart snapshot"
                )
                declares_external_supervisor = True
            if declares_external_supervisor and (pre_restart_pids is None or pid not in pre_restart_pids):
                externally_supervised_pids.add(pid)
            continue
        # Unmapped gateways have no profile home to query. The explicit argv
        # marker is still enough to identify a supervisor-owned launch.
        try:
            argv = _capture_gateway_argv(pid)
        except Exception:
            logger.debug("Could not read gateway argv for PID %s", pid, exc_info=True)
            argv = None
        if argv and "--external-supervisor" in argv and (pre_restart_pids is None or pid not in pre_restart_pids):
            externally_supervised_pids.add(pid)
    return externally_supervised_pids
