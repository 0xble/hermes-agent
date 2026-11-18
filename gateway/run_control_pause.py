"""Shared legacy/bootstrap update-pause control contract."""

import logging
import os
import threading


logger = logging.getLogger(__name__)


def make_update_pause_handler(runner, main_loop):
    def pause_for_update(params: dict) -> dict:
        # A detached updater cannot inherit the creating client's lifetime pipe.
        if getattr(runner, "_client_owned", False):
            return {"pausing": False, "client_owned": True}
        # ``{"force": true}`` (hermes update --force): stop live work now instead of waiting for idle.
        force = params.get("force") is True
        try:
            from hermes_cli.gateway import _get_restart_drain_timeout
            drain = 0.0 if force else float(_get_restart_drain_timeout())
        except Exception:
            logger.warning("Could not read restart drain timeout", exc_info=True)
            drain = 30.0
        accepted_box: list = []
        done = threading.Event()

        def request() -> None:
            try:
                from gateway.restart_when_idle import force_restart, request_service_restart
                accepted_box.append(force_restart(runner) if force else request_service_restart(runner))
                accepted_box.append(runner._describe_active_work())
            finally:
                done.set()

        main_loop.call_soon_threadsafe(request)
        done.wait(timeout=5.0)
        accepted = bool(accepted_box and accepted_box[0])
        return {"pausing": accepted, "already_stopping": not accepted,
                "pid": os.getpid(), "drain_timeout": drain, "force": force,
                "waiting_on": accepted_box[1] if len(accepted_box) > 1 else []}
    return pause_for_update
