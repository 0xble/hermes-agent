"""Session-only startup for the gateway leased by one finite local client.

This is not daemon enrollment: do not restore old admissions, connect messaging
adapters, self-provision relay, dispatch startup hooks, or run heartbeat/cron.
Provider execution, MCP discovery and the local session authority remain normal.
"""
from __future__ import annotations

import asyncio


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
    runner._suppress_clean_shutdown_receipt = False
    runner._running = True
    runner._update_runtime_status(runner._serving_state())
    return True
