"""Private review inbox. No model tool: curator consumers use this API or module CLI.

Rows are immutable evidence; disposition never applies a proposal. IDs deduplicate
kind/payload/source across retries, including already disposed observations.
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3

from hermes_constants import get_hermes_home

_source: ContextVar[dict | None] = ContextVar("review_observation_source", default=None)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def bind_review_source(session_id, messages):
    snapshot = _json(messages)
    token = _source.set({"session_id": session_id if isinstance(session_id, str) else None,
                         "snapshot_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
                         "messages": json.loads(snapshot)})
    try:
        yield
    finally:
        _source.reset(token)


@contextmanager
def _db():
    folder = get_hermes_home() / "state"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "review-observations.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            source TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', disposition_note TEXT NOT NULL DEFAULT '')""")
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_observation(kind, payload):
    if kind not in ("memory", "skills") or not isinstance(payload, dict):
        raise ValueError("Expected memory/skills and a proposal object")
    source = _source.get() or {"session_id": None, "snapshot_sha256": None, "messages": []}
    identity = hashlib.sha256(_json([kind, payload, source]).encode()).hexdigest()
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO observations (id,kind,payload,source,created_at) VALUES (?,?,?,?,?)",
                   (identity, kind, _json(payload), _json(source), datetime.now(timezone.utc).isoformat()))
        return _row(db.execute("SELECT * FROM observations WHERE id=?", (identity,)).fetchone())


def _row(row):
    keys = ("id", "kind", "payload", "source", "created_at", "status", "disposition_note")
    result = dict(zip(keys, row))
    for key in ("payload", "source"):
        result[key] = json.loads(result[key])
    return result


def list_observations(kind=None, status: str | None = "pending"):
    with _db() as db:
        return [_row(row) for row in db.execute(
            "SELECT * FROM observations WHERE (? IS NULL OR kind=?) AND (? IS NULL OR status=?) ORDER BY created_at,id",
            (kind, kind, status, status))]


def dispose(observation_id, status, note=""):
    if status not in ("accepted", "rejected", "deferred", "pending"):
        raise ValueError("Disposition must be accepted, rejected, deferred, or pending")
    with _db() as db:
        return db.execute("UPDATE observations SET status=?,disposition_note=? WHERE id=?",
                          (status, note, observation_id)).rowcount == 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--kind", choices=("memory", "skills"))
    listing.add_argument("--status", default="pending")
    disposition = commands.add_parser("dispose")
    disposition.add_argument("id")
    disposition.add_argument("status", choices=("accepted", "rejected", "deferred", "pending"))
    disposition.add_argument("--note", default="")
    args = parser.parse_args()
    result = (list_observations(args.kind, None if args.status == "all" else args.status)
              if args.command == "list" else {"success": dispose(args.id, args.status, args.note)})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
