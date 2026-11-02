"""Gateway install CLI options and systemd prompts, outside the gateway facade."""
from __future__ import annotations

import sys


def launchd_options(args, *, backend, system, run_as_user) -> dict:
    mode = getattr(args, "launchd_domain", None)
    if mode is None:
        return {}
    if backend != "launchd" or system or run_as_user:
        raise ValueError("--launchd-domain is a macOS per-user option; do not combine with --system/--run-as-user")
    if getattr(args, "start_on_login", None) is False:
        raise ValueError("--launchd-domain owns login registration; --no-start-on-login is not supported")
    return {"domain": mode}


def install_systemd(args, *, force: bool, system: bool, run_as_user) -> None:
    from hermes_cli import gateway as gw
    if gw.is_wsl():
        gw.print_warning("WSL detected — systemd services may not survive WSL restarts.")
        gw._print_info_lines(
            "  Consider running in foreground instead: hermes gateway run",
            "  Or use tmux/screen for persistence: tmux new -s hermes 'hermes gateway run'",
        )
        print()
    # Honor --start-now/--start-on-login; else prompt on a TTY, default True headless.
    non_interactive = not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty())

    def _flag(name: str, question: str) -> bool:
        value = getattr(args, name, None)
        if value is not None:
            return value
        return gw.prompt_yes_no(question, True) if not non_interactive else True

    start_now = _flag("start_now", "Start the gateway now after installing the service?")
    start_on_login = _flag("start_on_login", "Start the gateway automatically on login/boot with systemd?")
    gw.systemd_install(
        force=force, system=system, run_as_user=run_as_user,
        enable_on_startup=start_on_login, non_interactive=non_interactive,
        force_unit_path=getattr(args, "force_unit_path", False),
    )
    if start_now:
        gw.systemd_start(system=system)
