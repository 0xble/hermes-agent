"""Replay shutdown spools under each owning gateway profile at startup."""

import logging
from pathlib import Path
from functools import partial

from hermes_constants import get_routing_process_hermes_home
from gateway.session_recovery import SessionRecoveryMixin
from gateway.shutdown_flush import DROP_PENDING, OTHER_PLATFORM_PENDING, recover_pending_spool

logger = logging.getLogger("gateway.run")
_NOT_SUPPLIED = object()


def pending_home_for_key(runner, session_key: str) -> Path | None:
    """Resolve queued keys with the SessionStore's canonical namespace parser.

    Without multiplexing, legacy ``agent:main`` belongs to the launch profile,
    including when that profile is named. With multiplexing, it names default.
    """
    launch = Path(get_routing_process_hermes_home())
    primary = getattr(runner, "_primary_profile_name", None) or "default"
    served = getattr(runner, "_served_profile_homes", None) or {}
    config = getattr(runner, "config", None)
    multiplex = getattr(config, "multiplex_profiles", len(served) > 1)
    owner = SessionRecoveryMixin._profile_from_session_key(session_key)
    if not multiplex:
        return launch if owner in (None, "default", primary) else None
    if owner == primary:
        return launch
    return Path(served[owner]) if owner in served else None


def _defer_followup(runner, eligible, platform, key, session_id, data, path, *,
                    breaker_tripped=False, reconnect_recovery=False, recovered_events=None):
    # A queued message is a future turn; appending it to the interrupted transcript
    # makes the recovery note answer that message instead.
    if platform is not None and key in eligible and eligible[key].origin.platform != platform:
        return OTHER_PLATFORM_PENDING
    drain_deferred = data.get("drain_deferred") is True
    if reconnect_recovery and drain_deferred and getattr(runner, "_draining", False):
        return None  # shutdown still owns these arrivals
    if key not in eligible and not breaker_tripped and not drain_deferred:
        return False
    if breaker_tripped and not hasattr(runner.session_store, "_lock"):
        return False
    if any(getattr(event, "_hermes_recovery_spool", None) == path
           for event in getattr(runner, "_startup_restore_queue", ())):
        return OTHER_PLATFORM_PENDING  # claimed by this process already
    store = runner.session_store
    with store._lock:  # noqa: SLF001 — inspect the same snapshot as the breaker
        store._ensure_loaded_locked()  # noqa: SLF001
        entry = store._entries.get(key)  # noqa: SLF001
        if breaker_tripped and entry and entry.resume_pending and entry.session_id == session_id:
            return None
        if drain_deferred:
            if not entry or entry.session_id != session_id or not entry.origin or entry.suspended:
                return None  # keep the spool until its session can be safely restored
        elif not (entry and entry is eligible.get(key) and entry.resume_pending
                  and entry.session_id == session_id and entry.origin):
            return False
    from gateway.run import (
        _auto_continue_freshness_window, _is_fresh_gateway_interruption,
        _resume_pending_marker_timestamp,
    )
    if not drain_deferred:
        marker = _resume_pending_marker_timestamp(entry)
        if not _is_fresh_gateway_interruption(marker, window_secs=_auto_continue_freshness_window()):
            return False
    if runner._is_session_running(key):
        return None if drain_deferred else False
    source = runner._restored_source(entry)
    if drain_deferred and platform is not None and source.platform != platform:
        return OTHER_PLATFORM_PENDING
    authorization = runner._resume_owner_authorized(key, source)
    if authorization is not True:
        if drain_deferred:
            if authorization is None:
                return None  # transient check failure: keep the only copy for retry
            logger.warning("Dropping unauthorized drain-deferred message from %s", path)
            return DROP_PENDING
        return False
    ready = ((runner._delivery_adapter_for(source), source) if drain_deferred else
             runner._auto_resume_ready(entry, require_adapter=False))
    if ready is None:
        return False
    # Older spools lack authorship; never guess who issued a command in shared chats.
    author_id = data.get("source_user_id") or data.get("user_id")
    if not data.get("internal") and (not isinstance(author_id, str) or not author_id.strip()):
        return False
    adapter, source = ready
    if adapter is None:
        return None
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session_identity import replace_source
    message_id = data.get("message_id")
    source = replace_source(
        source, message_id=message_id, user_id=author_id,
        user_name=data.get("source_user_name") or data.get("user_name"),
        user_id_alt=data.get("source_user_id_alt"),
        is_bot=bool(data.get("source_is_bot", False)),
        role_authorized=bool(data.get("source_role_authorized", False)),
    )
    event = MessageEvent(
        text=data["text"], message_type=MessageType.TEXT, source=source,
        user_id=data.get("user_id") or author_id,
        user_name=data.get("user_name") or source.user_name,
        message_id=message_id,
        media_urls=data.get("media_urls") or data.get("media") or [],
        media_types=data.get("media_types") or [],
        reply_to_message_id=data.get("reply_to_message_id") or data.get("reply_to"),
        internal=bool(data.get("internal", False)),
        allow_gateway_control=bool(data.get("allow_gateway_control", True)),
        metadata=data.get("metadata") or {},
    )
    setattr(event, "_hermes_recovered_followup", True)
    setattr(event, "_hermes_recovery_spool", path)
    if recovered_events is not None:
        recovered_events.append(event)
    else:
        # Compatibility for direct callers that are already on the gateway loop.
        runner._queue_startup_restore_event(event)
    return True


def recover_pending_shutdown_flush(runner, *, candidates=_NOT_SUPPLIED, platform=None, recovered_events=None) -> int:
    """Visit the launch home and every served home; leave failed spools for a later boot.

    When *recovered_events* is supplied, worker-thread recovery only returns events through that
    list; the caller owns queue mutation on the gateway loop.
    """
    from gateway.run import _profile_runtime_scope

    # Snapshot once per recovery pass: the loop breaker must not be counted per payload.
    if candidates is _NOT_SUPPLIED:
        candidates = runner._resume_pending_candidates() if hasattr(runner, "_resume_pending_candidates") else []
    eligible = {entry.session_key: entry for entry in (candidates or [])}
    launch_home = Path(get_routing_process_hermes_home())
    homes = [launch_home, *((getattr(runner, "_served_profile_homes", None) or {}).values())]
    recovered = 0

    for home in dict.fromkeys(Path(home) for home in homes):
        try:
            if not (home / "pending_messages").is_dir() or not any((home / "pending_messages").glob("*.json")):
                continue
            with _profile_runtime_scope(home, prepared_secret_scope={}):
                def resolve_here(key, *, not_after=None):
                    owner_home = pending_home_for_key(runner, key)
                    if owner_home is None or (home != launch_home and owner_home != home):
                        return None
                    # Before profile-owned spools, the shared primary adapter and runner both
                    # wrote routed secondary slots into the launch home's spool. Only that
                    # legacy location may cross homes, and only for a verified served owner.
                    with _profile_runtime_scope(owner_home, prepared_secret_scope={}):
                        return runner.session_store.resolve_session_id_for_key(key, not_after=not_after)

                count, held_back = recover_pending_spool(
                    session_resolver=resolve_here,
                    deferred_followup=partial(_defer_followup, runner, eligible, platform,
                                              breaker_tripped=candidates is None,
                                              reconnect_recovery=platform is not None,
                                              recovered_events=recovered_events))
                recovered += count
                # A held-back session's next live row must drain its spool first, or it lands ahead.
                if held_back:
                    runner.session_store.mark_spooled_drop_sessions(held_back)
        except Exception:
            logger.warning("Pending-message recovery failed for profile home %s; spool retained", home,
                           exc_info=True)
    return recovered
