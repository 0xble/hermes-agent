"""The compressed fallback keeps the original private-journal resource bounds."""
import gzip
import json
import stat

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


@pytest.mark.parametrize('kind', ['plain', 'gzip', 'bomb', 'truncated', 'scope', 'over_disk'])
def test_journal_reopen_codec_and_resource_bounds(tmp_path, kind):
    scope = {'session_id': 's'}
    journal = {'scope': scope, 'next_sequence': 1, 'pending': []}
    data = json.dumps(journal).encode()
    expected = None
    if kind == 'scope':
        journal['scope'] = {'session_id': 'other'}
        data = gzip.compress(json.dumps(journal).encode())
        expected = WorkerPersistenceError
    elif kind == 'gzip':
        data = gzip.compress(data)
    elif kind == 'bomb':
        data = gzip.compress(b' ' * 10_000_000)
        expected = WorkerPersistenceError
    elif kind == 'truncated':
        data = gzip.compress(data)[:-4]
        expected = EOFError
    elif kind == 'over_disk':
        data = b' ' * 501
        expected = WorkerPersistenceError
    (tmp_path / 'pending.json').write_bytes(data)
    # Bomb fixture must fit disk to test the independent decode bound.
    limit = 100_000 if kind == 'bomb' else 500
    if expected:
        with pytest.raises(expected):
            RuntimeSessionStore(lambda *a, **kw: {}, scope, tmp_path, max_bytes=limit)
    else:
        store = RuntimeSessionStore(lambda *a, **kw: {'value': True}, scope, tmp_path, max_bytes=limit)
        try:
            assert store.journal == journal
            store.queue_token_counts('s', input_tokens=1)
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        finally:
            store.close()
    # Failed opens must release owner.lock too.
    if expected:
        (tmp_path / 'pending.json').write_text(json.dumps({'scope': scope, 'next_sequence': 1, 'pending': []}), encoding='utf-8')
        RuntimeSessionStore(lambda *a, **kw: {}, scope, tmp_path, max_bytes=limit).close()
