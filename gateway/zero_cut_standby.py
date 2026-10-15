"""Local readiness and promotion state for a zero-cut standby gateway slot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.gateway_slot import effective_gateway_standby, effective_gateway_slot

logger = logging.getLogger("gateway.zero_cut_standby")
_ready = False
_promoted = False
_runner = None


def is_standby() -> bool:
    """Return whether this process is still the pinned, opt-in standby slot."""
    return effective_gateway_standby() and not _promoted


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


async def _connect_deferred(runner, platform, adapter) -> bool:
    """Connect one deferred adapter; a raise counts as a failed connect so promotion still completes."""
    try:
        return bool(await runner._connect_initial_adapter_with_timeout(adapter, platform))
    except Exception:
        logger.exception("Promotion connect raised for deferred adapter %s", platform.value)
        return False


def _queue_secondary_reconnect(runner, profile, platform, adapter) -> None:
    """Hand a failed secondary to the profile's own reconnect slot, as a retryable fatal would."""
    pending = runner._profile_failed_platforms
    if not isinstance(pending, dict):
        pending = runner._profile_failed_platforms = {}
    profile_pending = pending.setdefault(profile, {})
    if platform not in profile_pending:
        profile_pending[platform] = runner._retain_background_task(asyncio.create_task(
            runner._run_secondary_profile_reconnect(profile, platform, adapter),
            name=f"secondary-reconnect:{profile}:{platform.value}"))


async def promote(runner) -> dict[str, Any]:
    """Promote a ready standby in place, starting deferred adapters and autonomous services.

    A deferred platform that fails to connect (False or a raise) goes to the same reconnect path a
    transient startup failure uses (primary queue or the profile's own reconnect task), so a promoted
    gateway heals instead of silently serving without it. The payload names every failed platform.
    """
    global _promoted
    if _promoted:
        return {"promoted": True, "already": True}
    if not is_standby():
        return {"promoted": False, "refused": "not_standby"}
    if not _ready:
        return {"promoted": False, "refused": "not_ready"}

    # The standby opened no shared store; take ownership before any adapter can deliver a turn.
    _promoted = True
    await runner._start_prime_session_db_after_ready()
    homes = list((getattr(runner, "_served_profile_homes", None) or {}).items())
    if homes:
        await asyncio.to_thread(runner._restore_secondary_completion_ledgers, homes)

    failed: list[str] = []
    deferred = dict(getattr(runner, "_standby_deferred", {}))
    runner._standby_deferred = {}
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
        if not await _connect_deferred(runner, platform, adapter):
            logger.error("Promotion could not connect deferred adapter %s; queued for reconnect", name)
            failed.append(name)
            if profile is None:
                runner._failed_platforms[platform] = runner._startup_retry_entry(platform, adapter, config)
            else:
                await runner._safe_adapter_disconnect(adapter, platform)
                _queue_secondary_reconnect(runner, profile, platform, adapter)
            continue
        if profile is None:
            runner._publish_primary_adapter(platform, adapter)
        else:
            runner._profile_adapters.setdefault(profile, {})[platform] = adapter

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
