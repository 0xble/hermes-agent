"""Shared legacy/bootstrap update-pause control contract."""

import logging
import os
import threading
from contextlib import suppress


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
        outcome: dict = {}
        done = threading.Event()

        def request() -> None:
            try:
                from gateway.restart_when_idle import force_restart, request_service_restart
                outcome["accepted"] = force_restart(runner) if force else request_service_restart(runner)
                describe = getattr(runner, "_describe_active_work", None)
                if callable(describe):
                    with suppress(Exception):
                        outcome["waiting_on"] = list(describe())
            finally:
                done.set()

        main_loop.call_soon_threadsafe(request)
        done.wait(timeout=5.0)
        accepted = bool(outcome.get("accepted"))
        ack = {"pausing": accepted, "already_stopping": not accepted,
               "pid": os.getpid(), "drain_timeout": drain}
        # Optional keys, present only when they say something: a forced stop, and the live work the
        # restart is waiting on. An ordinary pause of an idle gateway answers the four keys above.
        if force:
            ack["force"] = True
        if outcome.get("waiting_on"):
            ack["waiting_on"] = outcome["waiting_on"]
        return ack
    return pause_for_update
