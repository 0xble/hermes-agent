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

    config = read_home_selection(get_hermes_home()) or {}
    name = (config.get("memory") or {}).get("provider")
    if not name or not name.strip():
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
    manifest, _inputs = memory_provider_dependency_inputs(name.strip())
    packages = [Requirement(raw).name for raw in
                (manifest.get("python_dependencies") or manifest.get("pip_dependencies") or ())]
    detail = f" Client packages: {', '.join(packages)}." if packages else ""
    return (f"Memory provider {name} is unavailable: {reason}{detail} "
            "Run `hermes pm install` (or `hermes pm repair` for a broken client), then restart Hermes.")


def _marker(home: Path) -> Path:
    from hermes_cli.version_info import get_code_identity
    identity = get_code_identity().get("sha") or str(Path(__file__).resolve().parents[1])
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return home / "state" / "memory-client-health" / key


async def check_memory_health(runner) -> None:
    from gateway.run import _async_profile_runtime_scope, _handoff_watch_scopes
    from hermes_constants import get_hermes_home

    lock = getattr(runner, "_memory_health_lock", None)
    if lock is None:
        lock = runner._memory_health_lock = asyncio.Lock()
    if lock.locked():
        return
    async with lock:
        for profile, home in _handoff_watch_scopes(runner):
            home = Path(home or get_hermes_home())
            async with _async_profile_runtime_scope(home):
                await _check_profile_health(runner, profile, home)


def _pending_notice(home: Path) -> tuple[Path, str, bool]:
    marker = _marker(home)
    if marker.with_suffix(".notified").exists():
        return marker, "", False
    issue = _readiness_issue()
    if not issue:
        return marker, "", False
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.with_suffix(".logged").open("x", encoding="utf-8") as stream:
            stream.write(issue)
    except FileExistsError:
        logger.debug("Memory readiness error already logged for this release: %s", home)
        return marker, issue, False
    return marker, issue, True


async def _check_profile_health(runner, profile, home: Path) -> None:
    marker, issue, first = await asyncio.to_thread(_pending_notice, home)
    if not issue:
        return
    if first:
        logger.error("%s [profile=%s]", issue, home)
    for owner, platform, _config, channel, transport in runner._served_home_channel_transports():
        if owner != profile:
            continue
        if await runner._send_home_channel_message(
                platform, channel, transport, issue, "Memory health notice failed for %s:%s: %s"):
            await asyncio.to_thread(marker.with_suffix(".notified").touch)
            break


def schedule_memory_health(runner, loop) -> None:
    """The existing housekeeping thread triggers the same bounded boot check."""
    future = getattr(runner, "_memory_health_future", None)
    if future is not None and not future.done():
        return
    runner._memory_health_future = asyncio.run_coroutine_threadsafe(check_memory_health(runner), loop)


async def install_health_notices(runner) -> None:
    from gateway.cron_store_notices import install_cron_store_notices
    install_cron_store_notices(runner, asyncio.get_running_loop())
    await check_memory_health(runner)


def health_notice_chores(runner, loop) -> list:
    if runner is None or loop is None:
        return []
    return [
        (1, "Memory client readiness", lambda: schedule_memory_health(runner, loop), False),
        (1, "Update notice retry", lambda: runner._schedule_update_notice_retry(loop), False),
    ]
