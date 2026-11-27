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

from hermes_state_runtime import RuntimeStoreError

_SCALARS = (type(None), bool, int, float, str)


def _filters(raw):
    from hermes_state_maintenance import _PRUNE_FILTER_NAMES
    if (not isinstance(raw, dict) or set(raw) - (_PRUNE_FILTER_NAMES | {'older_than_days'})
            or not all(isinstance(value, _SCALARS) for value in raw.values())):
        raise RuntimeStoreError('invalid_params')
    return dict(raw)


async def prune(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    # Store-wide deletion across every principal's rows: the local operator only (a ticket from
    # this host's private control socket), never a remote or browser grant.
    if not connection.native_owner or 'session:operator' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    never_active = params.get('never_active_days')
    if ('filters' in params) == (never_active is not None) or (
            never_active is not None and (isinstance(never_active, bool) or not isinstance(never_active, (int, float))
                                          or never_active < 0)):
        raise RuntimeStoreError('invalid_params')
    filters = _filters(params['filters']) if 'filters' in params else None
    from hermes_state_prune_budget import validate_max_seconds
    try:
        max_seconds = validate_max_seconds(params.get("max_seconds"))
        if max_seconds is not None and filters is None:
            raise ValueError("max_seconds is only supported for retention pruning")
    except ValueError as exc:
        raise RuntimeStoreError("invalid_params", detail=str(exc)) from exc
    now = params.get('now') is True
    from hermes_cli.maintenance_window import deletion_window_refusal
    if not now and (refusal := deletion_window_refusal('pass --now')):
        raise RuntimeStoreError('outside_deletion_window', detail=refusal)
    authority._require_admission_open()
    from gateway.session_runtime_workers import track_mutation
    from hermes_cli.sessions_owner import PRUNE_RPC_TIMEOUT
    # The owner remains bounded even when its client disappears; its worker owns the
    # deadline, not an asyncio timeout (which cannot stop a running SQLite thread).
    # Reserve 10% of the client's timeout for settlement and returning the result.
    task = track_mutation(authority, _apply(authority, filters, never_active, not now,
                                          max_seconds if max_seconds is not None else PRUNE_RPC_TIMEOUT * 0.9))
    return await asyncio.shield(task)


async def _apply(authority, filters, never_active, within_window=False, max_seconds=None):
    from hermes_state_prune_pacing import DeletionWindowClosedError, PruneInProgressError
    try:
        return await _prune(authority, filters, never_active, within_window, max_seconds)
    except PruneInProgressError as exc:
        raise RuntimeStoreError('prune_in_progress', detail=str(exc)) from exc
    except DeletionWindowClosedError as exc:
        raise RuntimeStoreError('outside_deletion_window', detail=str(exc)) from exc


async def _prune(authority, filters, never_active, within_window, max_seconds=None):
    from hermes_constants import get_hermes_home
    from gateway.session_mutations import retire_live_sessions
    # Same transcript dir the CLI's local prune unlinks under (dispatch runs in the owner's scope).
    sessions_dir = Path(get_hermes_home()) / 'sessions'
    deleted_ids = []
    if filters is None:
        from hermes_state_prune_pacing import prune_pass

        def _never_active():
            with prune_pass(authority.db.db_path):
                return authority.db.prune_never_active_keyed_sessions(
                    older_than_days=float(never_active), sessions_dir=sessions_dir, deleted_ids=deleted_ids)
        deleted, routing, skipped = await asyncio.to_thread(_never_active)
        result = {'deleted': deleted, 'routing_deleted': routing, 'skipped': skipped}
    else:
        deleted = await asyncio.to_thread(
            authority.db.prune_sessions, sessions_dir=sessions_dir, exclude_active_write_guards=True,
            deleted_ids=deleted_ids, within_deletion_window=within_window, max_seconds=max_seconds, **filters)
        result = {'deleted': deleted}
    retire_live_sessions(authority, deleted_ids)
    return result
