"""Replay shutdown spools under each owning gateway profile at startup."""

import logging
from pathlib import Path
from functools import partial

from hermes_constants import get_routing_process_hermes_home
from agent.turn_context import _same_trigger_content
from gateway.session_recovery import SessionRecoveryMixin
from gateway.shutdown_flush import (
    DROP_PENDING, OTHER_PLATFORM_PENDING, payload_media_urls, recover_pending_spool,
    recovered_message_type,
)

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
    origin = getattr(eligible.get(key), "origin", None) if key in eligible else None
    if platform is not None and origin is not None and origin.platform != platform:
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
    if data.get("queue_accepted") is True and data.get("message_id") and any(
        str(event.message_id) == str(data["message_id"])
        and _same_trigger_content(event.text, data.get("text", ""), data.get("text", ""))
        and runner._session_key_for_source(event.source) == key
        for event in getattr(runner, "_startup_restore_queue", ())
    ):
        return OTHER_PLATFORM_PENDING  # same logical input, distinct crash/retry spool
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
            # Legacy drain arrivals are also future input whose only copy may be this spool.
            from gateway.shutdown_flush import quarantine_pending_spool
            quarantine_pending_spool(path, "resume owner not authorized")
            return False
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
    return _claim_recovered_followup(runner, key, session_id, data, path, source, author_id, recovered_events)


def _claim_recovered_followup(runner, key, session_id, data, path, source, author_id, recovered_events):
    """Queue the rebuilt follow-up for replay, unless an earlier boot's turn already owns it."""
    followup = data.get("shutdown_follow_up") is True
    if followup and input_already_durable(
        runner.session_store, session_id, data.get("message_id"),
        data.get("text"), data.get("persist_text"), data.get("dedupe"),
    ):
        # A previous boot's turn persisted this input before the restart cut it: the turn's own
        # resume_pending marker owns it now, and replaying the prompt could repeat tool effects.
        logger.info("Dropping recovered follow-up for %s: its turn already persisted it", key)
        return DROP_PENDING
    event = _recovered_followup_event(data, source, author_id, key=key, session_id=session_id)
    setattr(event, "_hermes_recovered_followup", True)
    setattr(event, "_hermes_recovery_spool", path)
    if followup:
        setattr(event, "_hermes_recovery_session_key", key)
        setattr(event, "_hermes_recovery_session_id", session_id)
    if recovered_events is not None:
        recovered_events.append(event)
    else:
        # Compatibility for direct callers that are already on the gateway loop.
        runner._queue_startup_restore_event(event)
    return True


def input_already_durable(
    store, session_id, message_id, user_message=None, persist_user_message=None, dedupe=None,
) -> bool:
    """True when a turn wrote this id with the same logical input content.

    The transcript wrapper can compare content itself.  A direct SessionDB has the row lookup but
    only exposes the historical id-only probe, so compare its returned rows here instead.
    """
    probe = getattr(store, "has_platform_message_id", None)
    if not session_id or message_id is None or not callable(probe):
        return False
    try:
        tip_for = getattr(store, "_compression_tip_for_session_id", None)
        tip = tip_for(session_id) if callable(tip_for) else session_id
        session_ids = dict.fromkeys((session_id, tip))
        get_rows = getattr(store, "get_messages_by_platform_message_id", None)
        if dedupe != 2:
            return any(bool(probe(sid, str(message_id))) for sid in session_ids if sid)
        if user_message is not None or persist_user_message is not None:
            from agent.turn_context import _same_trigger_content
            if callable(get_rows):
                return any(
                    _same_trigger_content(row.get("content"), user_message, persist_user_message)
                    for sid in session_ids if sid
                    for row in get_rows(sid, str(message_id))
                )
            import inspect
            try:
                accepts_content = len(inspect.signature(probe).parameters) >= 4
            except (TypeError, ValueError):
                accepts_content = False
            if not accepts_content:
                return False
            return any(
                bool(probe(sid, str(message_id), user_message, persist_user_message))
                for sid in session_ids if sid
            )
        return any(bool(probe(sid, str(message_id))) for sid in session_ids if sid)
    except Exception:
        logger.debug("Recovered follow-up durability probe failed for %s", session_id, exc_info=True)
        return False


def _recovered_followup_event(data, source, author_id, *, key=None, session_id=None):
    """Rebuild the spooled inbound as a MessageEvent bound to its restored source."""
    from gateway.platforms.event import MessageEvent
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
        text=data.get("text") or "", message_type=recovered_message_type(data), source=source,
        user_id=data.get("user_id") or author_id,
        user_name=data.get("user_name") or source.user_name,
        message_id=message_id,
        media_urls=payload_media_urls(data),
        media_types=data.get("media_types") or [],
        media_text_inlined=data.get("media_text_inlined") or [],
        reply_to_message_id=data.get("reply_to_message_id") or data.get("reply_to"),
        reply_to_text=data.get("reply_to_text"),
        reply_to_author_id=data.get("reply_to_author_id"),
        reply_to_author_name=data.get("reply_to_author_name"),
        reply_to_is_own_message=data.get("reply_to_is_own_message") is True,
        internal=bool(data.get("internal", False)),
        allow_gateway_control=bool(data.get("allow_gateway_control", True)),
        metadata={**(data.get("metadata") or {}), **(
            {"gateway_session_key": key, "gateway_session_id": session_id,
             "gateway_session_strict": True} if key and session_id else {}
        )},
        reply_expected=data["reply_expected"] if isinstance(data.get("reply_expected"), bool) else None,
    )
    setattr(event, "_hermes_queue_checkpoint", data.get("queue_accepted") is True)
    event._outbox_admission_id = data.get("_outbox_admission_id")
    event._hermes_persist_text = data.get("persist_text")
    event._hermes_dedupe = data.get("dedupe")
    event.session_id = data.get("session_id") or session_id
    return event




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
                                              breaker_tripped=(
                                                  getattr(candidates, "breaker_tripped", False)
                                                  or candidates is None
                                              ),
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
