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
    listener conflict refusal, startup reconnect), in the served order, after the primary is live.

    A secondary that ends unserved (creation returned None or raised, or a duplicate claim was refused)
    is stamped ``<profile>:telegram`` needs-attention, so it is visibly unserved rather than dropped."""
    from gateway.config import Platform
    homes = getattr(runner, "_served_profile_homes", None) or {}
    claimed = _existing_claims(runner)
    tg = Platform.TELEGRAM.value

    def unserved(profile: str, why: str) -> None:
        failed.append(f"{profile}:{tg}")
        retrying = Platform.TELEGRAM in ((getattr(runner, "_profile_failed_platforms", None) or {}).get(profile) or {})
        if not retrying:
            runner._update_platform_runtime_status(
                f"{profile}:{tg}", platform_state="fatal", error_code="promotion_unserved",
                error_message=f"Telegram was not started at standby promotion: {why}", needs_attention=True)

    for profile in [p for p in homes if p in profiles]:
        try:
            await runner._start_one_profile_adapters(profile, homes[profile], claimed)
        except Exception:
            logger.exception("Promotion could not start deferred adapters for profile %s", profile)
        if Platform.TELEGRAM not in (runner._profile_adapters.get(profile) or {}):
            unserved(profile, "adapter unavailable or credential already claimed (see gateway log)")
    for profile in [p for p in profiles if p not in homes]:
        logger.error("Promotion has no served home for deferred profile %s", profile)
        unserved(profile, "no served home for this profile")


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
    try:
        return await _promote(runner)
    except BaseException:
        # Back to standby so a retry runs again, rather than answering "already" with nothing serving.
        _promoted = False
        logger.exception("Standby promotion failed; still standby, promotion may be retried")
        raise


async def _promote(runner) -> dict[str, Any]:
    await runner._start_prime_session_db_after_ready()
    # Replay the launch profile's durable delegation completions the standby left untouched at boot.
    from tools.process_registry import process_registry
    await asyncio.to_thread(process_registry.restore_completions)
    homes = list((getattr(runner, "_served_profile_homes", None) or {}).items())
    if homes:
        await asyncio.to_thread(runner._restore_secondary_completion_ledgers, homes)

    failed: list[str] = []
    deferred = getattr(runner, "_standby_deferred", None) or {}
    # Consume entries one at a time so a promotion that raises midway leaves the rest for a retry.
    for key in [k for k in list(deferred) if k[0] is None]:
        config = deferred[key]
        platform = key[1]
        # A raise leaves the entry deferred, so the retry that promote()'s rollback allows can start it.
        adapter = runner._create_adapter(platform, config)
        deferred.pop(key)
        if adapter is None:
            # Same classification an active boot makes (_start_prefilter_platforms): only a platform
            # that can heal without a config change joins the reconnect queue; either way it is flagged.
            from gateway.run_adapters import _adapter_unavailable_message
            heals = runner._adapter_may_heal(platform, config)
            logger.error("Promotion could not create deferred adapter %s", platform.value)
            failed.append(platform.value)
            runner._update_platform_runtime_status(
                platform.value, platform_state="retrying" if heals else "fatal",
                error_code="adapter_unavailable",
                error_message=_adapter_unavailable_message(platform, retrying=heals), needs_attention=True)
            if heals:
                runner._failed_platforms[platform] = runner._startup_retry_entry(platform, None, config)
            continue
        runner._wire_adapter_handlers(adapter)
        if not await _connect_deferred(runner, platform, adapter):
            logger.error("Promotion could not connect deferred adapter %s; queued for reconnect", platform.value)
            failed.append(platform.value)
            runner._failed_platforms[platform] = runner._startup_retry_entry(platform, adapter, config)
            continue
        runner._publish_primary_adapter(platform, adapter)
    secondary = sorted({profile for profile, _platform in deferred if profile is not None})
    for key in [k for k in list(deferred) if k[0] is not None]:
        deferred.pop(key)
    if secondary:
        await _promote_secondary_profiles(runner, secondary, failed)

    await runner._start_finish_wiring(len(runner.adapters))
    runner._start_spawn_background_watchers()
    from gateway.run import _start_gateway_start_cron_and_housekeeping
    runner._zero_cut_cron_handles = _start_gateway_start_cron_and_housekeeping(runner)
    _start_adopting(runner)
    return {"promoted": True, "already": False, "failed": failed}


_PREDECESSOR_POLL_S = 1.0


def _start_adopting(runner) -> None:
    runner._retain_background_task(asyncio.create_task(
        _adopt_predecessor_duties(runner), name="zero-cut-adopt-predecessor-duties"))


async def _adopt_predecessor_duties(runner) -> None:
    """Once the predecessor slot has exited: take the host gateway role it held and replay the durable
    outbox it may have left pending. Both wait for the exit, because the predecessor keeps the host lock
    and may still be sending its last finals until then (the outbox CAS keeps a replay from doubling)."""
    from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY, effective_gateway_slot, slot_lock_held
    from gateway.status import _get_process_hermes_home
    other = SLOT_ACTIVE if effective_gateway_slot() == SLOT_STANDBY else SLOT_STANDBY
    home = _get_process_hermes_home()
    while await asyncio.to_thread(slot_lock_held, home, other):
        await asyncio.sleep(_PREDECESSOR_POLL_S)
    from gateway.run import _claim_host_gateway_role, _refresh_host_gateway_record
    try:
        _claim_host_gateway_role()  # on the loop thread, so its SIGTERM cleanup handler installs
    except SystemExit:
        logger.error("Promoted gateway could not take the host gateway role (another gateway owns this host)")
    else:
        _refresh_host_gateway_record(runner)
    if getattr(runner.config, "durable_outbox_enabled", False):
        from gateway.outbox import recover_gateway_destinations
        await recover_gateway_destinations(runner)


def reset_for_tests() -> None:
    """Reset local readiness and promotion state for isolated tests."""
    global _ready, _promoted, _runner
    _ready = False
    _promoted = False
    _runner = None
