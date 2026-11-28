"""Mount a cold messaging-platform session into the authority for a local viewer (TUI-8).

A messaging session (Telegram, Weixin, Discord, ...) is published into the authority only when its
lane's first post-restart inbound runs ``_publish_route``, so ``session.resume`` of its row answered
``not_found`` until someone wrote to the lane again, on every gateway restart. Before the canonical
authority (#106742) a viewer opened any such row straight from ``state.db``; this restores that for
every cold row of this profile that carries a ``session_key`` and a messaging-platform source:

* the lane's routing entry still names the row: its persisted ``origin`` is the highest-fidelity
  source, exactly what that first inbound would have published;
* the route is gone or moved on (``/new``, reset, a pruned entry): the row's own ``origin_json``,
  else its columns, rebuild the source.

Either way the source is revived through ``_restored_source``, so delivery stays pinned to the
persisted receiving bot. A row the lane moved past mounts DETACHED (gateway/session_detached.py): the
lane's current key names the newer conversation, so the row runs on a key of its own instead, its
turns and Stop act on it alone and its replies reach its app viewers only, never the chat
(Teknium's ruling: a chat's ``/new`` never locks the apps out of its old conversation). A compression continuation is never
mounted under its physical id: the lane's turns publish to the lineage root (``logical_owner``), so
``session.resume`` maps it to that root, as normal admission does. Rows without a ``session_key`` (subagents) have nothing to rebuild a lane
from. LOCAL rows (cron, CLI, TUI) restore from their private receipt and API rows from their binding
(``restore_api_session``); neither is ever mounted here. The mount semantics are the first inbound's
``_publish_route``: a row that ended for a recoverable reason (``agent_close``, ``ws_orphan_reap``,
...) is mounted live too, as the lane's next message would reopen it.
"""
from datetime import datetime

from gateway.config import Platform
from gateway.session_persistence import authority_persistence, owner_rule
from persistence.authority.vocabulary import LEGACY_LOCAL_SOURCES


def _messaging_platform(label):
    """The messaging ``Platform`` a row's ``source`` label names; None for the API transport (its
    own restore) and for labels no platform answers to (the CLI/TUI/cron local labels)."""
    try:
        platform = Platform(label)
    except ValueError:
        return None
    return None if platform in (Platform.LOCAL, Platform.API_SERVER) else platform


def _route_snapshot(authority, sid, route):
    """The current route entry, its valid match for *sid*, and whether it names another owner.

    This is a blocking read and is called only while building the off-loop mount plan. The returned
    route entry and version let the event-loop mount revalidate the snapshot without reacquiring the
    storage lock there.
    """
    store = authority.runner.session_store
    with store._lock:
        entry = store._entry_locked(route)
    owner = authority.logical_owner(entry.session_id) if entry is not None else None
    routed = (
        entry if entry is not None and entry.origin is not None and entry.session_key == route
        and owner == sid else None
    )
    return entry, routed, entry is not None and owner != sid


def _routed_entry(authority, sid, route):
    """The routing entry that still names *sid* under *route*, else None."""
    return _route_snapshot(authority, sid, route)[1]


def _row_source(row, platform, profile):
    """The row's persisted origin, else its columns (``channel_directory`` reads rows the same way)."""
    from gateway.session import SessionSource
    try:
        return SessionSource.from_dict(dict(row.origin))
    except (TypeError, ValueError, KeyError):
        pass
    if row.chat_id is None:
        return None
    source = SessionSource(
        platform=platform, chat_id=str(row.chat_id), user_id=row.user_id,
        chat_type=row.chat_type or 'dm', thread_id=row.thread_id,
        chat_name=row.display_name)
    if profile is not None:
        source.profile = profile
    return source


def _names_another_conversation(authority, sid, route):
    """The lane's current routing entry names a conversation other than *sid* (``/new``, reset)."""
    store = authority.runner.session_store
    with store._lock:
        entry = store._entry_locked(route)
    return entry is not None and authority.logical_owner(entry.session_id) != sid


def _row_entry(row, platform, route, profile):
    """A routing entry rebuilt from the row alone (the route no longer vouches for it)."""
    from gateway.session import SessionEntry
    source = _row_source(row, platform, profile)
    if source is None or source.platform != platform:
        return None
    started = datetime.fromtimestamp(row.started_at) if row.started_at else datetime.now()
    return SessionEntry(session_key=route, session_id=row.session_id, created_at=started, updated_at=started,
                        origin=source, platform=platform, chat_type=source.chat_type,
                        transport_profile=row.transport_profile)


def _observed_mount_plan(sid, route, entry):
    """Build a mount plan from the route entry the owner probe already observed.

    Used only when the cold row read fails during /stop or /new. It does no storage read, so a
    transient failure cannot discard a known owner before its queued admissions are settled. The
    on-loop reconcile compares *entry* with the live route: if the lane was repointed meanwhile,
    this entry becomes the detached row entry and the old session mounts detached.
    """
    if entry is None or entry.origin is None or entry.session_key != route:
        return None
    return {
        'sid': sid,
        'route': route,
        'entry': entry,
        'row_entry': entry,
        'route_entry': entry,
        'route_version': getattr(entry, '_route_version', None),
        'moved': False,
    }


def _mount_plan(authority, sid):
    """A read-only snapshot for mounting a row a route or messaging-platform row can stand for.

    The route snapshot is consumed on the gateway loop, where it is reconciled against the store's
    in-memory route version before publication. The row-derived entry is kept as the safe detached
    fallback if the lane moves while this worker is reading storage.
    """
    row = authority_persistence(authority).sessions.get(sid)
    route = row.session_key if row is not None else None
    platform = _messaging_platform(row.source) if route else None
    if platform is None or authority.logical_owner(sid) != sid:
        return None  # a compression continuation: its lineage root is the mount (session.resume maps it)
    store = authority.runner.session_store
    registry = getattr(authority.runner, 'session_authorities', None)
    profile = store._named_profile_for_key(route)
    if profile != (registry.profile_name(authority) if registry is not None else None):
        return None
    route_entry, routed, moved = _route_snapshot(authority, sid, route)
    row_entry = _row_entry(row, platform, route, profile)
    entry = routed or row_entry
    return None if entry is None else {
        'sid': sid,
        'route': route,
        'entry': entry,
        'row_entry': row_entry,
        'route_entry': route_entry,
        'route_version': getattr(route_entry, '_route_version', None),
        'moved': moved,
    }


def _reconcile_mount_plan(authority, plan):
    """Reconcile a worker snapshot with route changes already published on the loop."""
    store = authority.runner.session_store
    route = plan['route']
    current = store._entries.get(route)
    expected = plan['route_entry']
    same_snapshot = (
        current is expected
        or (current is not None and expected is not None
            and current._route_version == plan['route_version'])
    )
    if same_snapshot:
        return plan['entry'], plan['moved']
    if current is None:
        # The lane had an entry when the worker read storage and has none now: the row was retired,
        # deleted or pruned in between. The cached row snapshot no longer proves a mountable session.
        return None, False
    if current.session_id == plan['sid'] and current.origin is not None and current.session_key == route:
        return current, False
    return plan['row_entry'], True


def restore_routed_session(authority, sid, *, plan=None):
    """Publish *sid* as its lane would after a restart; None when this row is not one a route or a
    messaging-platform row can stand for."""
    from gateway.session_authority import LiveSession
    from gateway.session_contract import SessionRef
    plan = _mount_plan(authority, sid) if plan is None else plan
    if plan is None:
        return None
    entry, detached = _reconcile_mount_plan(authority, plan)
    if entry is None:
        return None
    route = plan['route']
    authority._require_admission_open()
    # Re-pin the persisted receiving bot so delivery after a restart goes through it or nowhere,
    # never the default bot by heuristic (gateway/AGENTS.md § Identity survives the process).
    source = authority.runner._restored_source(entry)
    if sid not in authority.sessions:
        live = authority.sessions[sid] = LiveSession(source, route)
        if detached:
            from gateway.session_detached import detach
            # The lane moved past it while the worker read storage, or had already moved before the
            # snapshot. Its turns stay off the chat, even though publication remains on this loop.
            detach(authority, live, sid)
    return SessionRef(authority.profile_id, sid)


def mountable(authority, sid):
    """``restore_routed_session`` would mount *sid* (this profile's messaging lineage root)."""
    return _mount_plan(authority, sid) is not None


def messaging_owner_ref(authority, ref):
    """The mount a messaging row resumes on: its compression lineage root, where the lane's turns
    publish their events (``logical_owner``). Any other row keeps *ref*."""
    row = authority_persistence(authority).sessions.get(ref.session_id)
    if row is None or not row.session_key or _messaging_platform(row.source) is None:
        return ref
    from gateway.session_contract import SessionRef
    owner = authority.logical_owner(ref.session_id)
    return ref if owner == ref.session_id else SessionRef(ref.profile_id, owner)


def handed_off_ref(authority, session_id):
    """The mount of *session_id* when it is a local conversation a messaging lane took over
    (``/handoff``, a platform ``/resume``): created by a local surface, now routed on a lane this
    authority mounts. Its title stays the conversation's, so ``/resume <title>`` and the Bots roster's
    registry lookup open it on that lane (#143286). None for any other row."""
    from gateway.session_contract import SessionRef
    from session_rules.legacy_local import client_label
    ref = messaging_owner_ref(authority, SessionRef(authority.profile_id, session_id))
    row = authority_persistence(authority).sessions.get(ref.session_id)
    created = row.created_source if row is not None else None
    # An adopted phone-app chat keeps the app's label as its created_source (bind_legacy_target).
    if (created not in LEGACY_LOCAL_SOURCES and not client_label(created, owner_rule())) or not mountable(authority, ref.session_id):
        return None
    return ref


def superseded_hand_off(authority, ref):
    """Whether the lane that took over *ref* (a ``handed_off_ref``) has since moved on to another
    conversation (``/new``, a reset): the chat is history now, read-only on that lane."""
    live = authority.sessions.get(ref.session_id)
    row = authority_persistence(authority).sessions.get(ref.session_id)
    route = row.session_key if row is not None else None
    # The routing entry moves at the platform's /new; the lane publishes its new conversation (and so
    # supersedes a mounted one) only at its next inbound.
    return bool(live is not None and live.detached) or _names_another_conversation(authority, ref.session_id, route)


async def conversation_head(runner, session_id):
    """``(authority, local owner or None, row id)`` a repoint of *session_id* onto a messaging lane
    carries (``/handoff``, a platform ``/resume``): a local conversation's current head
    (``bindings.handoff_head``), never an earlier ``/new`` segment, which would move that
    history onto the lane while the live conversation stays local, its receipt released (#143286)."""
    from gateway.session_authorities import active_authority
    authority = active_authority(runner)
    store = authority_persistence(authority) if authority is not None else runner.persistence
    if store is None:
        return authority, None, session_id
    return (authority, *await store.aio.bindings.handoff_head(session_id))


async def settled_handoff_db(runner, authority, session_id):
    """Refuse the watcher's repoint while durable work still owns *session_id*
    (``check_handoff_settled``), on the store ``conversation_head`` reads."""
    from gateway.session_handoff import check_settled
    store = authority_persistence(authority) if authority is not None else runner.persistence
    await check_settled(store, session_id)


def follow_hand_off(authority, source, route, owner, local_owner):
    """After ``/handoff`` repointed *route* at *owner* (the lane's logical id of the conversation's
    head): the local mount a viewer still holds (*local_owner*, the Desktop that ran it) moves onto
    the lane, as its receipt did. When the head is a later ``/new`` segment, that mount is the
    conversation's earlier history there, superseded once the head publishes (#143286)."""
    held = authority.sessions.get(local_owner) if local_owner else None
    if held is None or held.source is None or held.source.platform != Platform.LOCAL:
        held = None
    elif local_owner != owner:
        held.source, held.route = source, route
    if held is not None or owner in authority.sessions:
        authority._publish_route(source, owner, route)
