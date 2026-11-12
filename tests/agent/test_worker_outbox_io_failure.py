"""Capacity preflight is recoverable; actual journal I/O uncertainty is not."""
import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


@pytest.mark.parametrize('fail_at', [1, 2])
def test_journal_io_failure_stays_sticky(tmp_path, monkeypatch, fail_at):
    import utils
    calls = []
    def owner(method, **params):
        calls.append(params)
        return {'value': True}
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'outbox')
    original = utils.atomic_write_bytes
    writes = 0
    def fail(path, data, **kwargs):
        nonlocal writes
        writes += 1
        if writes == fail_at:
            raise OSError('disk failure')
        return original(path, data, **kwargs)
    monkeypatch.setattr(utils, 'atomic_write_bytes', fail)
    try:
        with pytest.raises(OSError, match='disk failure'):
            store.queue_token_counts('s', input_tokens=1)
        assert len(calls) == fail_at - 1
        assert store.failure == 'disk failure'
        with pytest.raises(WorkerPersistenceError, match='disk failure'):
            store.refresh_session_turn_lease('s', 'holder')
        with pytest.raises(WorkerPersistenceError, match='disk failure'):
            store.flush_token_counts()
        assert len(calls) == fail_at - 1
    finally:
        store._outbox_owner.close()
