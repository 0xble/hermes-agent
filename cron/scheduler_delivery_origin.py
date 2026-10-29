"""Relay egress discriminators a cron fire carries from its persisted job origin."""

from __future__ import annotations

from typing import Any


def stamp_origin_discriminators(t: Any, route_metadata: dict, media_metadata: dict) -> None:
    """Stamp the origin's ``scope_id`` / ``user_id`` onto a live send's metadata.

    Relay egress is fail-closed on a discriminator and the RelayAdapter's caches are cold after every
    boot, so the persisted origin supplies them. Origin targets only (a fan-out target's recipient is not
    the origin's author); ``setdefault`` never overrides router or home stamping; ``user_id`` is read by
    relay transports only.
    """
    discriminators = (
        ("scope_id", t.origin.get("scope_id") if t.origin_target else None),
        ("user_id", t.origin_user_id if t.is_relay else None),
    )
    for key, value in discriminators:
        if value:
            route_metadata.setdefault(key, str(value))
            media_metadata.setdefault(key, str(value))


def unresolved_delivery_outcome(job: dict, for_failure: bool) -> str | None:
    """``_deliver_result`` outcome when no target resolved: None (not a failure) for ``local`` and
    origin-less ``origin`` (CLI jobs never capture an origin — a spurious error every run), else
    an error string."""
    from cron.scheduler_delivery import _normalize_deliver_value, _delivery_lane_value
    from cron.scheduler_delivery import logger

    deliver_value = _normalize_deliver_value(_delivery_lane_value(job, for_failure=for_failure))
    if deliver_value == "local":
        return None
    if deliver_value == "origin":
        logger.info(
            # deliver=origin with no resolvable origin and no configured home channels: treat as local
            # rather than reporting an error. CLI-created jobs never capture a {platform, chat_id} origin,
            # so failing here would make every CLI `deliver=origin` (or auto-detect) job emit a spurious "no
            # delivery target resolved" error on every run (#43014). The output is still persisted in
            # last_output for `cron list`/resume.
            "Job '%s': deliver=origin but no origin or home channels — "
            "skipping delivery (output saved in last_output)",
            job.get("name", job.get("id", "?")))
        return None
    msg = f"no delivery target resolved for deliver={deliver_value}"
    logger.warning("Job '%s': %s", job["id"], msg)
    return msg
