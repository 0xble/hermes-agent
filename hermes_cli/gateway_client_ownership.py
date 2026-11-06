"""Client lease ownership shared by update pause, restart and survivor selection."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

_reported_client_pids: set[int] = set()


def is_client_owned_gateway(
    pid: int, identity: dict | None = None, *,
    home: Path | str | None = None, argv: list[str] | None = None,
) -> bool:
    """A PID-matched control identity or exact argv flag proves a finite client lease.

    No argv substring heuristics, and never trust another PID's answer on a reused
    home socket. Older/unreachable sockets fall back to the captured launch argv.
    Callers may pass identity/argv already read during discovery.
    """
    if pid <= 1:
        return False
    if identity is None:
        with suppress(Exception):
            from gateway.control_socket import identify_gateway
            from hermes_cli.update_fleet_scope import gateway_pid_home

            home = home or gateway_pid_home(pid)
            if home:
                identity = identify_gateway(Path(home))
    if identity and identity.get("pid") == pid and identity.get("client_owned"):
        return True
    if argv is None:
        with suppress(Exception):
            from hermes_cli.gateway import _capture_gateway_argv

            argv = _capture_gateway_argv(pid)
    return bool(argv and "--client-owned" in argv)


def leave_client_owned_gateway(pid: int, identity: dict | None = None, **context) -> bool:
    """Exclude a proven lease from updater stop/replay debt, with one notice per PID."""
    if not is_client_owned_gateway(pid, identity, **context):
        return False
    if pid not in _reported_client_pids:
        print(f"  ↷ left client-owned gateway {pid} on its lease")
        _reported_client_pids.add(pid)
    return True
