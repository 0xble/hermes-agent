"""Session-only startup for the gateway leased by one finite local client.

This is not daemon enrollment: do not restore old admissions, connect messaging
adapters, self-provision relay, dispatch startup hooks, or run heartbeat/cron.
Provider execution, MCP discovery and the local session authority remain normal.
"""
from __future__ import annotations

import asyncio


async def start_runner_for_owner(runner) -> bool:
    """Select finite-session or ordinary daemon enrollment inside start() cleanup."""
    import logging
    logging.getLogger("gateway.run").info("Starting Hermes Gateway...")
    if getattr(runner, "_client_owned", False):
        return await start_client_owned_runner(runner)
    return await runner._start_impl()


def config_for_owner(config, client_owned):
    """Finite local owners must not enroll copied daemon platform configuration."""
    if not client_owned:
        return config
    import copy
    config = copy.deepcopy(config)
    config.platforms = {}
    return config


def arm_owner_ready_services(runner, *, idle_exit: bool, client_owned: bool) -> None:
    """Client idle owners arm retirement; only persistent daemons prewarm the UI."""
    if idle_exit:
        from gateway.run_idle_exit import arm_unmanaged_idle_exit
        arm_unmanaged_idle_exit(runner)
    elif not client_owned:
        from gateway.run_api import warm_gateway_dashboard
        warm_gateway_dashboard(runner)


def start_daemon_lifecycle(*, client_owned: bool, standby: bool):
    """Record recovery/enroll keepalive only for a daemon, never a finite owner."""
    if client_owned:
        return None
    from gateway.run import _best_effort

    def record_startup():
        from gateway.lifecycle_ledger import record_startup
        return record_startup()

    def start_keepalive():
        from hermes_cli.nous_auth_keepalive import start_nous_auth_keepalive
        start_nous_auth_keepalive()

    unclean_exit = (None if standby else
                    _best_effort(record_startup, "Lifecycle ledger startup record failed: %s"))
    _best_effort(start_keepalive, "Nous auth keepalive did not start: %s")
    return unclean_exit


async def start_client_owned_runner(runner) -> bool:
    from hermes_cli.plugins import discover_plugins
    from hermes_loop_guard import set_loop_thread
    import threading

    runner._gateway_loop = asyncio.get_running_loop()
    set_loop_thread(threading.get_ident())
    await asyncio.to_thread(discover_plugins, require_complete=True)
    # Tool-level hooks use the usual consent policy; daemon gateway:startup hooks do not run.
    runner._register_launch_profile_config_hooks()
    runner.hooks.discover_and_load()
    runner._mcp_discovery_ready = asyncio.Event()
    runner._startup_restore_in_progress = False
    runner._startup_restore_queue = []
    runner._startup_restore_tasks = []
    runner._start_mcp_discovery()
    await runner._await_mcp_discovery()  # first/only turn must see the normal bounded tool snapshot
    from hermes_startup_watchdog import disarm_startup_watchdog
    disarm_startup_watchdog()
    # This path never recovered the previous daemon: it cannot certify a clean daemon exit.
    runner._suppress_clean_shutdown_receipt = True
    runner._running = True
    runner._update_runtime_status(runner._serving_state())
    return True
