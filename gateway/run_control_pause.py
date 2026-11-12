"""Shared legacy/bootstrap update-pause control contract."""

import logging
import os
import threading


logger = logging.getLogger(__name__)


def make_update_pause_handler(runner, main_loop):
    def pause_for_update() -> dict:
        # A detached updater cannot inherit the creating client's lifetime pipe.
        if getattr(runner, "_client_owned", False):
            return {"pausing": False, "client_owned": True}
        try:
            from hermes_cli.gateway import _get_restart_drain_timeout
            drain = float(_get_restart_drain_timeout())
        except Exception:
            logger.warning("Could not read restart drain timeout", exc_info=True)
            drain = 30.0
        accepted_box: list[bool] = []
        done = threading.Event()

        def request() -> None:
            try:
                accepted_box.append(runner.request_restart(detached=False, via_service=True))
            finally:
                done.set()

        main_loop.call_soon_threadsafe(request)
        done.wait(timeout=5.0)
        accepted = bool(accepted_box and accepted_box[0])
        return {"pausing": accepted, "already_stopping": not accepted,
                "pid": os.getpid(), "drain_timeout": drain}
    return pause_for_update
