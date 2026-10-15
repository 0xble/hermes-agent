"""Local readiness and promotion state for a slot-b zero-cut standby gateway."""

from __future__ import annotations

import logging
from typing import Any

from gateway.gateway_slot import SLOT_STANDBY, effective_gateway_slot

logger = logging.getLogger("gateway.zero_cut_standby")
_ready = False
_promoted = False
_runner = None


def is_standby() -> bool:
    """Return whether this process is still the pinned, opt-in standby slot."""
    return effective_gateway_slot() == SLOT_STANDBY and not _promoted


def mark_ready() -> None:
    """Mark the local gateway ready after its runner has started successfully."""
    global _ready
    _ready = True


def defer_platform(runner, platform, config, profile: str | None = None) -> bool:
    """Record a standby platform without constructing it; return whether it was deferred."""
    if not is_standby():
        return False
    global _runner
    _runner = runner
    deferred = getattr(runner, "_standby_deferred", None)
    if deferred is None:
        deferred = runner._standby_deferred = {}
    deferred[(profile, platform)] = config
    return True


def readiness() -> dict[str, Any]:
    """Return local readiness fields exposed by the control-socket status."""
    slot = effective_gateway_slot()
    runner_deferred = []
    if _runner is not None:
        entries = getattr(_runner, "_standby_deferred", {})
        runner_deferred = [
            f"{profile}:{platform.value}" if profile else platform.value
            for profile, platform in entries
        ]
    return {"slot": slot, "standby": is_standby(), "ready": _ready, "deferred": runner_deferred}


async def promote(runner) -> dict[str, Any]:
    """Promote a ready standby in place, starting deferred adapters and autonomous services.

    A deferred primary platform that fails to connect is queued for the reconnect watcher, the
    same as a transient failure at normal startup, so a promoted gateway heals instead of
    silently serving without Telegram. The payload names every failed platform.
    """
    global _promoted
    if _promoted:
        return {"promoted": True, "already": True}
    if not is_standby():
        return {"promoted": False, "refused": "not_standby"}
    if not _ready:
        return {"promoted": False, "refused": "not_ready"}

    failed: list[str] = []
    deferred = dict(getattr(runner, "_standby_deferred", {}))
    for (profile, platform), config in deferred.items():
        name = f"{profile}:{platform.value}" if profile else platform.value
        adapter = runner._create_adapter(platform, config)
        if adapter is None:
            logger.error("Promotion could not create deferred adapter %s", name)
            failed.append(name)
            continue
        if profile is None:
            runner._wire_adapter_handlers(adapter)
        else:
            runner._configure_profile_adapter(adapter, profile, platform)
        if not await runner._connect_initial_adapter_with_timeout(adapter, platform):
            logger.error("Promotion could not connect deferred adapter %s", name)
            failed.append(name)
            if profile is None:
                runner._failed_platforms[platform] = runner._startup_retry_entry(platform, adapter, config)
            else:
                await runner._safe_adapter_disconnect(adapter, platform)
            continue
        if profile is None:
            runner._publish_primary_adapter(platform, adapter)
        else:
            runner._profile_adapters.setdefault(profile, {})[platform] = adapter

    _promoted = True
    runner._standby_deferred = {}
    await runner._start_finish_wiring(len(runner.adapters))
    runner._start_spawn_background_watchers()
    from gateway.run import _start_gateway_start_cron_and_housekeeping
    runner._zero_cut_cron_handles = _start_gateway_start_cron_and_housekeeping(runner)
    return {"promoted": True, "already": False, "failed": failed}


def reset_for_tests() -> None:
    """Reset local readiness and promotion state for isolated tests."""
    global _ready, _promoted, _runner
    _ready = False
    _promoted = False
    _runner = None
