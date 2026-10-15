"""Local readiness and promotion state for a slot-b zero-cut standby gateway."""

from __future__ import annotations

import asyncio
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


async def _connect_deferred(runner, platform, adapter) -> bool:
    """Connect one deferred adapter; a raise counts as a failed connect so promotion still completes."""
    try:
        return bool(await runner._connect_initial_adapter_with_timeout(adapter, platform))
    except Exception:
        logger.exception("Promotion connect raised for deferred adapter %s", platform.value)
        return False


def _existing_claims(runner) -> dict:
    """Credential/listener claims already held by live primary and secondary adapters."""
    active = getattr(runner, "_primary_profile_name", None) or "default"
    claimed = runner._primary_resource_claims(active)
    for profile, adapters in (getattr(runner, "_profile_adapters", None) or {}).items():
        for platform, adapter in adapters.items():
            for claim in (runner._adapter_credential_claim(platform, adapter),
                          runner._adapter_listener_claim(platform, adapter)):
                if claim is not None:
                    claimed.setdefault(claim, profile)
    return claimed


async def _promote_secondary_profiles(runner, profiles: list[str], failed: list[str]) -> None:
    """Start deferred secondaries through the normal per-profile path (runtime scope, credential and
    listener conflict refusal, startup reconnect), in the served order, after the primary is live."""
    from gateway.config import Platform
    homes = getattr(runner, "_served_profile_homes", None) or {}
    claimed = _existing_claims(runner)
    for profile in [p for p in homes if p in profiles]:
        try:
            await runner._start_one_profile_adapters(profile, homes[profile], claimed)
        except Exception:
            logger.exception("Promotion could not start deferred adapters for profile %s", profile)
        if Platform.TELEGRAM not in (runner._profile_adapters.get(profile) or {}):
            failed.append(f"{profile}:{Platform.TELEGRAM.value}")
    for profile in [p for p in profiles if p not in homes]:
        logger.error("Promotion has no served home for deferred profile %s", profile)
        failed.append(f"{profile}:{Platform.TELEGRAM.value}")


async def promote(runner) -> dict[str, Any]:
    """Promote a ready standby in place, starting deferred adapters and autonomous services.

    A deferred primary that fails to connect (False or a raise) joins the primary reconnect queue;
    deferred secondaries start through the normal per-profile path, so a promoted gateway heals
    instead of silently serving without them. The payload names every platform not yet serving.
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
        if profile is not None:
            continue
        adapter = runner._create_adapter(platform, config)
        if adapter is None:
            logger.error("Promotion could not create deferred adapter %s", platform.value)
            failed.append(platform.value)
            continue
        runner._wire_adapter_handlers(adapter)
        if not await _connect_deferred(runner, platform, adapter):
            logger.error("Promotion could not connect deferred adapter %s; queued for reconnect", platform.value)
            failed.append(platform.value)
            runner._failed_platforms[platform] = runner._startup_retry_entry(platform, adapter, config)
            continue
        runner._publish_primary_adapter(platform, adapter)
    secondary = sorted({profile for profile, _platform in deferred if profile is not None})
    if secondary:
        await _promote_secondary_profiles(runner, secondary, failed)

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
