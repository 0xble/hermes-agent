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


def readiness_for_runner(runner) -> dict[str, Any]:
    """Return readiness with the runner's deferred platform names when available."""
    result = readiness()
    entries = getattr(runner, "_standby_deferred", {})
    result["deferred"] = [
        f"{profile}:{platform.value}" if profile else platform.value
        for profile, platform in entries
    ]
    return result


async def promote(runner) -> dict[str, Any]:
    """Promote a ready standby in place, starting deferred adapters and autonomous services."""
    global _promoted
    if _promoted:
        return {"promoted": True, "already": True}
    if not is_standby():
        return {"promoted": False, "refused": "not_standby"}
    if not _ready:
        return {"promoted": False, "refused": "not_ready"}

    deferred = dict(getattr(runner, "_standby_deferred", {}))
    for (profile, platform), config in deferred.items():
        adapter = runner._create_adapter(platform, config)
        if adapter is None:
            logger.warning("Promotion could not create deferred adapter %s:%s", profile, platform.value)
            continue
        if profile is None:
            runner._wire_adapter_handlers(adapter)
            connected = await runner._connect_initial_adapter_with_timeout(adapter, platform)
            if not connected:
                await runner._safe_adapter_disconnect(adapter, platform)
                continue
            runner._publish_primary_adapter(platform, adapter)
        else:
            profile_map = (getattr(runner, "_profile_adapters", None) or {}).setdefault(profile, {})
            runner._configure_profile_adapter(adapter, profile, platform)
            connected = await runner._connect_initial_adapter_with_timeout(adapter, platform)
            if not connected:
                await runner._safe_adapter_disconnect(adapter, platform)
                continue
            profile_map[platform] = adapter

    _promoted = True
    runner._standby_deferred = {}
    await runner._start_finish_wiring(len(runner.adapters))
    runner._start_spawn_background_watchers()
    from gateway.run import _start_gateway_start_cron_and_housekeeping
    runner._zero_cut_cron_handles = _start_gateway_start_cron_and_housekeeping(runner)
    return {"promoted": True, "already": False}


def reset_for_tests() -> None:
    """Reset local readiness and promotion state for isolated tests."""
    global _ready, _promoted, _runner
    _ready = False
    _promoted = False
    _runner = None
