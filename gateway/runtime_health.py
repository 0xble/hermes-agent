"""One actionable memory-readiness alarm per profile and release, not per message."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _readiness_issue() -> str:
    from hermes_constants import get_hermes_home
    from pm.plugins_state import read_home_selection
    from plugins.memory import load_memory_provider
    from agent.memory_provider import is_core_memory_provider

    config = read_home_selection(get_hermes_home()) or {}
    name = (config.get("memory") or {}).get("provider")
    if is_core_memory_provider(name):
        return ""
    provider = load_memory_provider(name.strip(), register_skills=False)
    if provider is None:
        return f"Memory provider {name}: could not load configured provider. Run `hermes pm install` and restart Hermes."
    if provider.is_available():
        return ""
    reason = getattr(provider, "unavailable_reason", lambda: "")()
    # #845 owns client readiness. Do not mistake absent credentials (no client
    # degradation reason) for a dropped dependency or duplicate its checks here.
    if not reason:
        return ""
    from hermes_cli.memory_setup import memory_provider_dependency_inputs
    from packaging.requirements import Requirement
    packages = []
    try:
        manifest, _inputs = memory_provider_dependency_inputs(name.strip())
        packages = [Requirement(raw).name for raw in
                    (manifest.get("python_dependencies") or manifest.get("pip_dependencies") or ())]
    except Exception:
        # Optional package names enrich the warning, never gate its delivery.
        logger.debug("Could not read memory client package hints for %s", name, exc_info=True)
    detail = f" Client packages: {', '.join(packages)}." if packages else ""
    return (f"Memory provider {name} is unavailable: {reason}{detail} "
            "Run `hermes pm install` (or `hermes pm repair` for a broken client), then restart Hermes.")


def _marker(home: Path) -> Path:
    from hermes_cli.version_info import get_code_identity
    identity = get_code_identity().get("sha") or str(Path(__file__).resolve().parents[1])
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return home / "state" / "memory-client-health" / key


def _failure_key(home: Path) -> str:
    try:
        return str(_marker(home))
    except Exception:
        return f"{__file__}:{home}"


async def check_memory_health(runner) -> None:
    from gateway.run import _async_profile_runtime_scope, _resolve_handoff_watch_scopes
    from hermes_constants import get_default_hermes_root

    lock = getattr(runner, "_memory_health_lock", None)
    if lock is None:
        lock = runner._memory_health_lock = asyncio.Lock()
    if lock.locked():
        return
    async with lock:
        for profile, home in await _resolve_handoff_watch_scopes(runner):
            try:
                home = Path(home or get_default_hermes_root())
                async with _async_profile_runtime_scope(home):
                    await _check_profile_health(runner, profile, home)
            except Exception:
                # Advisory probes must not block startup or the remaining profiles.
                failures = getattr(runner, "_memory_health_failures", None)
                if failures is None:
                    failures = runner._memory_health_failures = set()
                key = (profile, await asyncio.to_thread(_failure_key, home))
                log = logger.debug if key in failures else logger.error
                failures.add(key)
                log("Memory readiness check failed [profile=%s, home=%s]",
                    profile, home, exc_info=True)


def _pending_notice(home: Path) -> tuple[Path, str, bool]:
    from utils import atomic_write_text

    marker = _marker(home)
    if marker.with_suffix(".notified").exists() or marker.with_suffix(".resolved").exists():
        return marker, "", False
    logged = marker.with_suffix(".logged")
    if logged.exists():
        # Defer the fresh readiness check until delivery is possible, so muted
        # profiles do not reload providers on each tick or gateway restart.
        saved = logged.read_text(encoding="utf-8-sig")
        if saved.strip():
            return marker, saved, False
    issue = _readiness_issue()
    if not issue:
        return marker, "", False
    atomic_write_text(logged, issue)
    return marker, issue, True


async def _check_profile_health(runner, profile, home: Path) -> None:
    from gateway.warning_notifications import present_notification

    marker, issue, first = await asyncio.to_thread(_pending_notice, home)
    if not issue:
        return
    if first:
        logger.error("%s [profile=%s]", issue, home)
    checked = first
    for owner, platform, _config, channel, transport in runner._served_home_channel_transports():
        if owner != profile:
            continue
        sent = False

        async def send() -> None:
            nonlocal sent, issue, checked
            if not checked:
                issue = await asyncio.to_thread(_readiness_issue)
                checked = True
                if not issue:
                    await asyncio.to_thread(marker.with_suffix(".resolved").touch)
                    await asyncio.to_thread(marker.with_suffix(".logged").unlink, missing_ok=True)
            if not issue:
                return
            sent = await runner._send_home_channel_message(
                platform, channel, transport, issue, "Memory health notice failed for %s:%s: %s")

        await present_notification(send, platform=platform)
        if not issue:
            break
        # Muted diagnostics remain log-only, not delivered. Keep the notice eligible
        # if the owning profile later enables warning notifications for this release.
        if sent:
            await asyncio.to_thread(marker.with_suffix(".notified").touch)
            break


def _log_memory_health_failure(done) -> None:
    if done.cancelled():
        return
    error = done.exception()
    if error is not None:
        logger.error("Periodic memory readiness check failed",
                     exc_info=(type(error), error, error.__traceback__))


def schedule_memory_health(runner, loop) -> None:
    """The existing housekeeping thread triggers the same bounded boot check."""
    future = getattr(runner, "_memory_health_future", None)
    if future is not None and not future.done():
        return
    runner._memory_health_future = asyncio.run_coroutine_threadsafe(check_memory_health(runner), loop)
    runner._memory_health_future.add_done_callback(_log_memory_health_failure)


async def install_health_notices(runner) -> None:
    """Notice setup is best-effort, never a prerequisite for gateway availability."""
    try:
        from gateway.cron_store_notices import install_cron_store_notices
        install_cron_store_notices(runner, asyncio.get_running_loop())
    except Exception:
        logger.error("Cron store health notice setup failed", exc_info=True)
    try:
        schedule_memory_health(runner, asyncio.get_running_loop())
    except Exception:
        logger.error("Memory health notice setup failed", exc_info=True)


def health_notice_chores(runner, loop) -> list:
    if runner is None or loop is None:
        return []
    return [
        (1, "Memory client readiness", lambda: schedule_memory_health(runner, loop), False),
        (1, "Update notice retry", lambda: runner._schedule_update_notice_retry(loop), False),
    ]
