"""Real temporary-home reservations for epoch-writing test fixtures.

Outside pytest's isolation context these helpers never acquire ownership. An ad
hoc importer must satisfy the writer's normal ownership contract itself.
"""
import os
from pathlib import Path

from gateway.runtime_ownership import exclusive_maintenance, process_ownership
from gateway.session_authority import initialize_session_authority as _initialize
from hermes_state import _ensure_test_isolation
from hermes_state_runtime import begin_runtime_epoch as _begin


def _home(db):
    path = Path(db.db_path).expanduser().resolve()
    _ensure_test_isolation(path)
    return path.parent


def begin_runtime_epoch(db, *, instance_id):
    if not os.environ.get('HERMES_TEST_ISOLATION'):
        return _begin(db, instance_id=instance_id)
    home = _home(db)
    if process_ownership.owns(home):
        return _begin(db, instance_id=instance_id)
    with exclusive_maintenance([home]):
        return _begin(db, instance_id=instance_id)


async def initialize_session_authority(runner, *, profile_id, instance_id, db=None, register=True):
    selected = db if db is not None else getattr(runner._session_db, '_db', runner._session_db)
    home = _home(selected) if os.environ.get('HERMES_TEST_ISOLATION') else None
    acquired = home is not None and not process_ownership.owns(home)
    if acquired:
        # The production initializer writes in asyncio.to_thread: maintenance's
        # thread-local reservation cannot authorize that worker thread.
        process_ownership.reserve([home])
    try:
        return await _initialize(runner, profile_id=profile_id, instance_id=instance_id,
                                 db=db, register=register)
    finally:
        if acquired and home is not None:
            process_ownership.release(home)
