"""Producer admission for the worker's local journal, without owner RPC preflights."""
from contextlib import contextmanager
import json


def open_public_outbox(store):
    from agent.runtime_session_store import WorkerPersistenceError
    from gateway.status import _try_acquire_file_lock
    from hermes_dir_acl import make_user_dir
    import os
    with journal_admission(store.scope):
        make_user_dir(store.path.parent, mode=0o700, parents=True, exist_ok=True)
        if store.path.parent.is_symlink() or store.path.is_symlink():
            raise WorkerPersistenceError('unsafe_outbox')
        os.chmod(store.path.parent, 0o700)
        lock_path = store.path.parent / 'owner.lock'
        if lock_path.is_symlink():
            raise WorkerPersistenceError('unsafe_outbox')
        owner = lock_path.open('a+', encoding='utf-8')
        try:
            os.chmod(lock_path, 0o600)
            if not _try_acquire_file_lock(owner):
                raise WorkerPersistenceError('outbox_in_use')
        except BaseException:
            owner.close()
            raise
        return owner


def _require_public(scope, journal=None, operation=None, payload=None):
    from agent.runtime_session_store import WorkerPersistenceError
    from hermes_state_runtime import RuntimeStoreError
    from hermes_state_runtime_privacy import require_public, worker_owners
    try:
        owners = (scope['session_id'],)
        if operation is not None:
            owners += worker_owners(scope['session_id'], operation, payload)
        if journal is not None:
            target = journal['scope']['session_id']
            owners += (target,)
            for entry in journal['pending']:
                owners += worker_owners(target, entry['operation'], entry['payload'])
        require_public(owners)
    except RuntimeStoreError as exc:
        raise WorkerPersistenceError(exc.reason) from None


@contextmanager
def journal_admission(scope, journal=None, *, operation=None, payload=None):
    # Never hold this gate over RPC: owner writers acquire DB lock, then shared gate.
    from agent.incognito import persistence_admission
    with persistence_admission():
        _require_public(scope, journal, operation, payload)
        yield


def private_convenience(scope):
    """Only the normal transcript/usage no-op surfaces suppress local privacy refusal."""
    from agent.runtime_session_store import WorkerPersistenceError
    try:
        _require_public(scope)
    except WorkerPersistenceError as exc:
        if str(exc) == 'privacy_denied':
            return True
        raise
    return False


def authoritative_privacy_denial(exc):
    from agent.runtime_session_store import WorkerPersistenceError
    from hermes_state_runtime import RuntimeStoreError
    return isinstance(exc, (WorkerPersistenceError, RuntimeStoreError)) and str(exc) == 'privacy_denied'


def discard_refused_entry(store, entry):
    # The owner explicitly refused BEFORE commit. This is not an ambiguous transport
    # failure: the same sequence remains available for the next public operation.
    candidate = dict(store.journal, pending=store.journal['pending'][1:], next_sequence=entry['sequence'])
    store._save(candidate)
    store.journal = candidate
    store.failure = None


def queue_entry(store, operation, payload, *, best_effort):
    from agent.runtime_session_store import WorkerPersistenceError
    # Caller holds the store lock and local privacy admission through the save.
    if store.failure:
        raise WorkerPersistenceError(store.failure)
    if store.journal['pending']:
        raise WorkerPersistenceError('pending_receipt')
    entry = {'sequence': store.journal['next_sequence'], 'operation': operation, 'payload': payload}
    candidate = json.loads(json.dumps(store.journal))
    candidate['pending'].append(json.loads(json.dumps(entry, allow_nan=False)))
    candidate['next_sequence'] += 1
    from agent.runtime_session_journal import OutboxCapacityError, check_worker_frame
    try:
        check_worker_frame(store.scope, candidate['pending'][-1])
        store._save(candidate)
    except OutboxCapacityError:
        # Optional archives may skip an unsent capacity rejection. Mandatory
        # append callers can swallow errors: retain failure for flush/finish.
        if not best_effort:
            store.failure = 'outbox_full'
        raise
    except Exception as exc:
        if not authoritative_privacy_denial(exc):
            store.failure = str(exc)
        raise
    store.journal = candidate

