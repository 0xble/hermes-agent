"""Fixtures shared by state.db maintenance tests."""

import pytest


@pytest.fixture(autouse=True)
def _open_auto_maintenance_window(monkeypatch):
    """Keep state.db auto-maintenance tests deterministic regardless of the wall clock."""
    import hermes_cli.maintenance_window

    monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: True)


@pytest.fixture
def worker(tmp_path):
    """A registered compute worker execution on session ``owned``: ``(db, RuntimeSessionStore)``."""
    # Imported here, not at module level: this conftest loads for every hermes_state test file.
    from agent.runtime_session_store import RuntimeSessionStore
    from hermes_state import SessionDB
    from hermes_state_runtime import mutate_worker_execution, register_worker_execution
    from tests.fakes.runtime_owner import begin_runtime_epoch

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'cli', system_prompt='prefix')
    db.create_session('foreign', 'cli', system_prompt='secret')
    epoch = begin_runtime_epoch(db, instance_id='fixture')
    scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
    store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')
    yield db, store
    store.failure = None
    store.journal['pending'] = []
    store.close()
    db.close()
