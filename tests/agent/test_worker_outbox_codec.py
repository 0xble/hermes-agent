"""The compressed fallback keeps the original private-journal resource bounds."""
import gzip
import json
import os
import stat

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


@pytest.mark.parametrize('encoding', ['plain', 'gzip'])
@pytest.mark.parametrize('payload', [b'{not json}', b'\xff'])
def test_invalid_journal_text_is_classified_as_corrupt(tmp_path, encoding, payload):
    from agent.runtime_session_journal import read_journal

    path = tmp_path / 'pending.json'
    path.write_bytes(gzip.compress(payload) if encoding == 'gzip' else payload)
    with pytest.raises(WorkerPersistenceError, match='^outbox_corrupt$') as error:
        read_journal(path, 500)
    assert isinstance(error.value.__cause__, (json.JSONDecodeError, UnicodeDecodeError))


def test_invalid_deflate_body_is_classified_as_corrupt(tmp_path):
    import zlib
    from agent.runtime_session_journal import read_journal

    valid = gzip.compress(b'{}', mtime=0)
    # BFINAL=1, BTYPE=3 is an invalid deflate block, independently of zlib version.
    path = tmp_path / 'pending.json'
    path.write_bytes(valid[:10] + b'\x07' + valid[-8:])
    with pytest.raises(WorkerPersistenceError, match='^outbox_corrupt$') as error:
        read_journal(path, 500)
    assert isinstance(error.value.__cause__, zlib.error)


@pytest.mark.parametrize('kind', ['plain', 'gzip', 'bomb', 'truncated', 'bad_crc', 'scope', 'over_disk'])
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
        expected = WorkerPersistenceError
    elif kind == 'bad_crc':
        data = gzip.compress(data)[:-8] + b'\x00' * 8
        expected = WorkerPersistenceError
    elif kind == 'over_disk':
        data = b' ' * 501
        expected = WorkerPersistenceError
    (tmp_path / 'pending.json').write_bytes(data)
    # Bomb fixture must fit disk to test the independent decode bound.
    limit = 100_000 if kind == 'bomb' else 500
    if expected:
        with pytest.raises(expected, match='outbox_corrupt' if kind in ('truncated', 'bad_crc') else None):
            RuntimeSessionStore(lambda *a, **kw: {}, scope, tmp_path, max_bytes=limit)
    else:
        store = RuntimeSessionStore(lambda *a, **kw: {'value': True}, scope, tmp_path, max_bytes=limit)
        try:
            assert store.journal == journal
            store.queue_token_counts('s', input_tokens=1)
            if os.name == 'posix':
                assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        finally:
            store.close()
    # Failed opens must release owner.lock too.
    if expected:
        (tmp_path / 'pending.json').write_text(json.dumps({'scope': scope, 'next_sequence': 1, 'pending': []}), encoding='utf-8')
        RuntimeSessionStore(lambda *a, **kw: {}, scope, tmp_path, max_bytes=limit).close()
