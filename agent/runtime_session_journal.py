"""Private retry-journal codec; receipt payloads are never rewritten or truncated."""
import gzip
import io
import json
import zlib

from agent.runtime_session_store import WorkerPersistenceError


class OutboxCapacityError(WorkerPersistenceError):
    """A definitely-unsent operation did not fit; the prior journal is intact."""


# Match the existing worker WebSocket budget; compression must not turn a
# disk-sized journal into an unbounded allocation or an unsendable RPC frame.
MAX_WORKER_FRAME_BYTES = 8 * 1024 * 1024


def encode_journal(journal, max_bytes):
    encoded = json.dumps(journal, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
    if len(encoded) > min(2 * max_bytes, MAX_WORKER_FRAME_BYTES):
        raise OutboxCapacityError('outbox_full')
    if len(encoded) > max_bytes:
        encoded = gzip.compress(encoded, mtime=0)
    if len(encoded) > max_bytes:
        raise OutboxCapacityError('outbox_full')
    return encoded


def read_journal(path, max_bytes):
    with path.open('rb') as source:
        encoded = source.read(max_bytes + 1)
    if len(encoded) > max_bytes:
        raise WorkerPersistenceError('outbox_full')
    if encoded.startswith(b'\x1f\x8b'):
        limit = min(2 * max_bytes, MAX_WORKER_FRAME_BYTES)
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as source:
                encoded = source.read(limit + 1)
        except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
            raise WorkerPersistenceError('outbox_corrupt') from exc
        if len(encoded) > limit:
            raise WorkerPersistenceError('outbox_full')
    try:
        return json.loads(encoded.decode('utf-8-sig'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkerPersistenceError('outbox_corrupt') from exc


def check_worker_frame(scope, entry):
    # The canonical owner separately caps the UTF-8 payload at 4 MiB. Do not
    # journal something its receipt transaction is guaranteed to refuse.
    payload = json.dumps(entry['payload'], ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
    if len(payload.encode('utf-8', errors='surrogatepass')) > 4 * 1024 * 1024:
        raise OutboxCapacityError('outbox_full')
    # WorkerRPC uses json.dumps' default spacing. Reserve a request id larger
    # than any practical run rather than assuming compact journal bytes = wire bytes.
    frame = {'jsonrpc': '2.0', 'id': 10 ** 64, 'method': 'worker.persist',
             'params': dict(scope, **entry)}
    if len(json.dumps(frame, allow_nan=False).encode()) > MAX_WORKER_FRAME_BYTES:
        raise OutboxCapacityError('outbox_full')


def save_journal(store, journal):
    if store._outbox_owner.closed:
        raise WorkerPersistenceError('outbox_closed')
    encoded = encode_journal(journal, store.max_bytes)
    from utils import atomic_write_bytes
    # Owner-only journal (the directory is 0700 too); rename and directory entry are durable.
    atomic_write_bytes(store.path, encoded, tmp_prefix='.pending-', mode=0o600, fsync_dir=True)
