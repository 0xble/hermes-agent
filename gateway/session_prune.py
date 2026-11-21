"""``session.prune``: the owner runs `hermes sessions prune` for its own store.

The CLI computes the filters and shows the preview from a read-only view; the delete itself is
this owner's write, through the same ``prune_sessions`` / ``retire_prunable`` path housekeeping
uses (busy sessions and the logical owner of a retained reset child are skipped). A CLI writing
the store under a live gateway would be a second writer, so it never does.

Deletes run only inside ``maintenance.deletion_window`` unless the operator passes ``now``, and one
pass per store at a time: the work is shielded, so a client that times out and asks again would
otherwise start a second pass beside the first.
"""
import asyncio
from pathlib import Path

from persistence.core.errors import AuthorityRefusal

_SCALARS = (type(None), bool, int, float, str)


def _filters(raw):
    from persistence.housekeeping.maintenance import PRUNE_FILTER_NAMES
    if (not isinstance(raw, dict) or set(raw) - (PRUNE_FILTER_NAMES | {'older_than_days'})
            or not all(isinstance(value, _SCALARS) for value in raw.values())):
        raise AuthorityRefusal('invalid_params')
    return dict(raw)


async def prune(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    # Store-wide deletion across every principal's rows: the local operator only (a ticket from
    # this host's private control socket), never a remote or browser grant.
    if not connection.native_owner or 'session:operator' not in actor.capabilities:
        raise AuthorityRefusal('permission_denied')
    if actor.profile_id != authority.profile_id:
        raise AuthorityRefusal('profile_mismatch')
    never_active = params.get('never_active_days')
    if ('filters' in params) == (never_active is not None) or (
            never_active is not None and (isinstance(never_active, bool) or not isinstance(never_active, (int, float))
                                          or never_active < 0)):
        raise AuthorityRefusal('invalid_params')
    filters = _filters(params['filters']) if 'filters' in params else None
    now = params.get('now') is True
    from hermes_cli.maintenance_window import deletion_window_refusal
    if not now and (refusal := deletion_window_refusal('pass --now')):
        raise RuntimeStoreError('outside_deletion_window', detail=refusal)
    authority._require_admission_open()
    from gateway.session_runtime_workers import track_mutation
    task = track_mutation(authority, _apply(authority, filters, never_active, not now))
    return await asyncio.shield(task)


async def _apply(authority, filters, never_active, within_window=False):
    from hermes_state_prune_pacing import DeletionWindowClosedError, PruneInProgressError
    try:
        return await _prune(authority, filters, never_active, within_window)
    except PruneInProgressError as exc:
        raise RuntimeStoreError('prune_in_progress', detail=str(exc)) from exc
    except DeletionWindowClosedError as exc:
        raise RuntimeStoreError('outside_deletion_window', detail=str(exc)) from exc


async def _prune(authority, filters, never_active, within_window):
    from hermes_constants import get_hermes_home
    from gateway.session_ingress_media import native_inputs_root
    from gateway.session_mutations import retire_live_sessions
    from gateway.session_persistence import authority_persistence
    from persistence.housekeeping.maintenance import PruneCriteria
    from session_retention import after_sessions_removed
    store = authority_persistence(authority)
    # Same transcript dir the CLI's local prune unlinks under (dispatch runs in the owner's scope).
    sessions_dir = Path(get_hermes_home()) / 'sessions'
    media_root = str(native_inputs_root())

    def prune_never_active():
        outcome = store.routing.prune_never_active(older_than_days=float(never_active), media_root=media_root)
        if outcome.deleted_ids or outcome.sessions_skipped:  # the delete ran: its post-commit work follows
            single = len(outcome.deleted_ids) == 1 and not outcome.sessions_skipped
            after_sessions_removed(store, store.scope.profile_dir, outcome.removed_ids, reason='delete',
                                   primary=outcome.deleted_ids[0] if single else None, sessions_dir=sessions_dir)
            store.routing.sweep_routing_entries_for_sessions(outcome.deleted_ids)
        return outcome

    def prune_sessions():
        bounds = {key: filters[key] for key in ('older_than_days', 'source') if key in filters}
        criteria = PruneCriteria(**{key: value for key, value in filters.items() if key not in bounds})
        outcome = store.maintenance.prune_sessions(
            **bounds, criteria=criteria, exclude_active_write_guards=True, media_root=media_root)
        after_sessions_removed(store, store.scope.profile_dir, outcome.deleted_ids, reason='prune',
                               sessions_dir=sessions_dir)
        return outcome

    if filters is None:
<<<<<<< HEAD
        from hermes_state_prune_pacing import prune_pass

        def _never_active():
            with prune_pass(authority.db.db_path):
                return authority.db.prune_never_active_keyed_sessions(
                    older_than_days=float(never_active), sessions_dir=sessions_dir, deleted_ids=deleted_ids)
        deleted, routing, skipped = await asyncio.to_thread(_never_active)
        result = {'deleted': deleted, 'routing_deleted': routing, 'skipped': skipped}
||||||| dff103e05878
        deleted, routing, skipped = await asyncio.to_thread(
            authority.db.prune_never_active_keyed_sessions, older_than_days=float(never_active),
            sessions_dir=sessions_dir, deleted_ids=deleted_ids)
        result = {'deleted': deleted, 'routing_deleted': routing, 'skipped': skipped}
=======
        outcome = await asyncio.to_thread(prune_never_active)
        result = {'deleted': outcome.sessions_deleted, 'routing_deleted': outcome.routing_entries_deleted,
                  'skipped': outcome.sessions_skipped}
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    else:
<<<<<<< HEAD
        deleted = await asyncio.to_thread(
            authority.db.prune_sessions, sessions_dir=sessions_dir, exclude_active_write_guards=True,
            deleted_ids=deleted_ids, within_deletion_window=within_window, **filters)
        result = {'deleted': deleted}
||||||| dff103e05878
        deleted = await asyncio.to_thread(
            authority.db.prune_sessions, sessions_dir=sessions_dir, exclude_active_write_guards=True,
            deleted_ids=deleted_ids, **filters)
        result = {'deleted': deleted}
=======
        outcome = await asyncio.to_thread(prune_sessions)
        result = {'deleted': outcome.count}
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    retire_live_sessions(authority, list(outcome.deleted_ids))
    return result
