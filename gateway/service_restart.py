"""Plan launchd reloads before bootout starts its non-extendable stop deadline."""

from __future__ import annotations

import asyncio
import logging
import os
import threading

logger = logging.getLogger(__name__)


def make_service_reload_handler(runner, main_loop):
    """Acknowledge only after the gateway has frozen admission on its loop."""
    def request() -> dict:
        accepted = []
        done = threading.Event()

        def start() -> None:
            try:
                accepted.append(runner.request_restart(via_service=True, service_reload=True))
            finally:
                done.set()

        main_loop.call_soon_threadsafe(start)
        done.wait(timeout=5.0)
        return {"accepted": bool(accepted and accepted[0]), "pid": os.getpid()}

    return request


async def reload_service_after_wait(runner) -> None:
    """Only now may launchd send SIGTERM: its ExitTimeOut cannot cover the active-work wait."""
    from hermes_cli.gateway import get_launchd_plist_path
    from hermes_cli.gateway_launchd import _reload_installed_launchd_plist

    try:
        result = await asyncio.to_thread(
            _reload_installed_launchd_plist, get_launchd_plist_path(), wait_for_active_work=False,
        )
        if result == "deferred":
            return  # The independent helper's bootout enters stop() under the normal shutdown watchdog.
    except Exception:
        logger.exception("Service reload failed after active-work wait; falling back to service restart")
    await runner.stop(restart=True, service_restart=True)
