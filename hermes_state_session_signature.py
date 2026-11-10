"""Bounded write-side invalidation and incremental session projection digests."""

from dataclasses import dataclass
import hashlib
import marshal
from pathlib import Path
import sqlite3

from hermes_state_errors import _STATE_DB_GENERATION_KEY


# Activity heartbeats are deliberately excluded. Liveness comes from active_list,
# while a real turn changes message_count. Keep the reader and triggers in sync.
SESSION_SIGNATURE_FIELDS = (
    "id", "source", "session_key", "display_name", "model", "parent_session_id",
    "started_at", "ended_at", "end_reason", "message_count", "tool_call_count",
    "cwd", "git_branch", "git_repo_root", "title", "title_source", "profile_name",
    "archived", "pinned", "hidden", "last_read_at", "handoff_state",
)

# A watcher that falls behind this bounded journal rebuilds from a snapshot.
# No consumer leases or unbounded tombstones survive stopped gateways.
SESSION_SIGNATURE_HISTORY = 4096
SESSION_SIGNATURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS session_projection_changes (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    session_rowid INTEGER NOT NULL,
    nonce BLOB NOT NULL DEFAULT (randomblob(32))
);
"""
_TRIM = f"""DELETE FROM session_projection_changes WHERE sequence <=
    (SELECT MAX(sequence) FROM session_projection_changes) - {SESSION_SIGNATURE_HISTORY};"""
_INSERT = "INSERT INTO session_projection_changes(session_rowid) VALUES (new.rowid);"
_DELETE = "INSERT INTO session_projection_changes(session_rowid) VALUES (old.rowid);"
_FIELDS_CHANGED = " OR ".join(
    f"old.{field} IS NOT new.{field} COLLATE BINARY OR typeof(old.{field}) IS NOT typeof(new.{field})"
    for field in SESSION_SIGNATURE_FIELDS
)
_TRIGGERS = {
    "sessions_projection_insert": f"AFTER INSERT ON sessions BEGIN {_INSERT} {_TRIM} END",
    "sessions_projection_delete": f"AFTER DELETE ON sessions BEGIN {_DELETE} {_TRIM} END",
    "sessions_projection_update": (
        f"AFTER UPDATE OF {', '.join(SESSION_SIGNATURE_FIELDS)} ON sessions "
        f"WHEN old.rowid IS new.rowid AND ({_FIELDS_CHANGED}) BEGIN "
        f"{_INSERT} {_TRIM} END"
    ),
    # Pseudo-column aliases are absent from the signature fields. Keep moves
    # disjoint from metadata updates, including a move and rename in one write.
    "sessions_projection_rowid_update": (
        "AFTER UPDATE OF rowid, _rowid_, oid ON sessions WHEN old.rowid IS NOT new.rowid BEGIN "
        f"{_DELETE} {_INSERT} {_TRIM} END"
    ),
}
_TRIGGER_DEFINITIONS = {
    name: f"CREATE TRIGGER {name} {body}" for name, body in _TRIGGERS.items()
}
# The schema owner's settled-trigger executor avoids unnecessary write locks.
SESSION_SIGNATURE_TRIGGER_SQL = "\n".join(
    f"DROP TRIGGER IF EXISTS {name};\n"
    f"CREATE TRIGGER IF NOT EXISTS {name} {body};\n"
    for name, body in _TRIGGERS.items()
)
_MASK = (1 << 256) - 1


@dataclass
class SessionProjection:
    identity: tuple
    sequence: int
    nonce: bytes | None
    hashes: dict[int, int]
    total: int


def session_signature_revision(conn: sqlite3.Connection, path: Path) -> tuple | None:
    """Trust the journal only with its exact triggers, within the digest's snapshot."""
    placeholders = ",".join("?" for _ in _TRIGGER_DEFINITIONS)
    installed = dict(conn.execute(
        f"SELECT name, sql FROM sqlite_master WHERE (type = 'trigger' AND name IN ({placeholders})) "
        "OR (type = 'table' AND name = 'state_meta')",
        tuple(_TRIGGER_DEFINITIONS),
    ))
    if installed.pop("state_meta", None) is None or installed != _TRIGGER_DEFINITIONS:
        return None
    generation = conn.execute("SELECT value FROM state_meta WHERE key = ?", (_STATE_DB_GENERATION_KEY,)).fetchone()
    if not generation or not generation[0]:
        return None
    first, last = conn.execute(
        "SELECT (SELECT MIN(sequence) FROM session_projection_changes), "
        "(SELECT MAX(sequence) FROM session_projection_changes)"
    ).fetchone()
    nonce = conn.execute("SELECT nonce FROM session_projection_changes WHERE sequence = ?", (last,)).fetchone()
    schema = conn.execute("PRAGMA schema_version").fetchone()[0]
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, schema, generation[0]), first or 0, last or 0, nonce[0] if nonce else None


def _row_hash(row) -> int:
    # Marshal v2 preserves scalar types, binary doubles and embedded NULs without
    # object memoization. Never unmarshal stored input.
    return int.from_bytes(hashlib.blake2b(marshal.dumps(tuple(row), 2), digest_size=32).digest(), "big")


def _signature(fields, total, count) -> tuple:
    # Modular addition supports duplicate rows (XOR would cancel equal pairs).
    # The 256-bit row fingerprints retain more bits than the old 128-bit
    # digest. Count distinguishes additions/removals independently.
    return fields, (total & _MASK).to_bytes(32, "big"), count


def session_content_signature(conn: sqlite3.Connection, path: Path, previous=None) -> tuple:
    """Full legacy scan or indexed changed-row reads. The caller owns one read transaction."""
    revision = session_signature_revision(conn, path)
    if revision is not None and previous is not None:
        identity, first, last, nonce = revision
        if (previous.identity == identity and first <= previous.sequence <= last):
            anchor = conn.execute("SELECT nonce FROM session_projection_changes WHERE sequence = ?",
                                  (previous.sequence,)).fetchone()
            # Sequence numbers alone can repeat in diverged copies restored
            # onto the same inode. Authenticate the cached journal position.
            if (anchor[0] if anchor else None) == previous.nonce:
                incremental = _incremental_signature(conn, previous, last, nonce)
                if incremental is not None:
                    return incremental
    return _full_signature(conn, revision)


def _full_signature(conn, revision):
    columns = conn.execute("PRAGMA table_info(sessions)").fetchall()
    available = {row[1] for row in columns}
    fields = tuple(field for field in SESSION_SIGNATURE_FIELDS if field in available)
    if not fields:
        return None, None
    order = " ORDER BY id" if "id" in available else ""
    canonical = (
        fields == SESSION_SIGNATURE_FIELDS
        and [row[1] for row in columns if row[5]] == ["id"]
        and conn.execute("SELECT 1 FROM sessions WHERE id IS NULL LIMIT 1").fetchone() is None
    )
    if not canonical:
        return _legacy_signature(conn, fields, order), None
    tracked = revision is not None
    prefix = "rowid, " if tracked else ""
    rows = conn.execute(f"SELECT {prefix}{', '.join(fields)} FROM sessions{order}")
    hashes, total, count = {}, 0, 0
    for row in rows:
        hashed = _row_hash(row[1:] if tracked else row)
        total += hashed
        count += 1
        if tracked:
            hashes[row[0]] = hashed
    state = SessionProjection(revision[0], revision[2], revision[3], hashes, total & _MASK) if tracked else None
    return _signature(fields, total, count), state


def _legacy_signature(conn, fields, order):
    # Without unique session identities a multiset digest could miss swapping
    # metadata between rows. Preserve the legacy reader's ordered byte stream.
    digest = hashlib.blake2b(digest_size=16)
    for row in conn.execute(f"SELECT {', '.join(fields)} FROM sessions{order}"):
        digest.update(marshal.dumps(tuple(row), 2))
        digest.update(b"\0")
    return fields, digest.digest()


def _incremental_signature(conn, previous, last, nonce):
    # Fetch and hash before mutating the cached state, so a busy/error path keeps
    # the entire last good baseline. Deduplicate repeated writes in one tick.
    changed = conn.execute(
        "SELECT DISTINCT session_rowid FROM session_projection_changes WHERE sequence > ?",
        (previous.sequence,),
    ).fetchall()
    updates = {}
    fields = SESSION_SIGNATURE_FIELDS
    for (rowid,) in changed:
        row = conn.execute(f"SELECT {', '.join(fields)} FROM sessions WHERE rowid = ?", (rowid,)).fetchone()
        if row is not None and row[0] is None:
            return None
        updates[rowid] = _row_hash(row) if row is not None else None
    if updates:
        expected_count = len(previous.hashes) + sum(
            (hashed is not None) - (rowid in previous.hashes) for rowid, hashed in updates.items()
        )
        # REPLACE can silently delete conflicting rows when recursive_triggers
        # is off, including a different row with the same UNIQUE title. Every
        # inserted/updated survivor is journaled, so an uncovered deletion can
        # only leave extra cached rows. Count on a covering index detects it.
        if conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] != expected_count:
            return None
    for rowid, hashed in updates.items():
        previous.total -= previous.hashes.pop(rowid, 0)
        if hashed is not None:
            previous.hashes[rowid] = hashed
            previous.total += hashed
    previous.total &= _MASK
    previous.sequence = last
    previous.nonce = nonce
    return _signature(fields, previous.total, len(previous.hashes)), previous
