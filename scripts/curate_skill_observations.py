#!/usr/bin/env python3
"""Index and disposition Markdown skill observations in a local SQLite store.

Supported commands are intentionally small: ``index`` imports top-level
observation files, ``disposition`` records an explicit decision, ``archive``
moves only dispositioned files, and ``list`` reports indexed rows. The former
worktree/PR curation interface is not supported by this command.

Each observation is one immutable file named ``<skill>@<suffix>.md``; writers
never append to an indexed file, so every record can be dispositioned and
archived on its own. The legacy ``<skill>.md`` form is still indexed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DISPOSITIONS = ("pending", "accepted", "rejected", "deferred")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class IndexResult:
    imported: int = 0
    existing: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class ArchiveResult:
    archived: int = 0
    pending: int = 0
    missing: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def observation_id(skill: str, payload: str) -> str:
    """Return the stable ID for one skill/payload pair."""
    return sha256_text(f"{skill}\0{payload}")


def default_observations() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "observations"


def default_db() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "state" / "skill-observations.sqlite3"


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY,
            skill TEXT NOT NULL,
            payload TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_mtime TEXT,
            provenance TEXT,
            disposition TEXT NOT NULL DEFAULT 'pending'
                CHECK (disposition IN ('pending', 'accepted', 'rejected', 'deferred')),
            disposition_reason TEXT,
            imported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            disposition_at TEXT,
            archive_path TEXT,
            archived_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS observations_skill_idx ON observations(skill)")
    conn.execute("CREATE INDEX IF NOT EXISTS observations_disposition_idx ON observations(disposition)")
    conn.commit()
    return conn


def _provenance(payload: str) -> str | None:
    """Read a simple optional front-matter provenance value without changing payload."""
    if not payload.startswith("---\n"):
        return None
    end = payload.find("\n---", 4)
    if end < 0:
        return None
    for line in payload[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() in {"provenance", "source", "origin"}:
            value = value.strip().strip('"\'')
            return value or None
    return None


def _files(observations: Path) -> Iterable[Path]:
    return sorted(path for path in observations.glob("*.md") if path.is_file())


def _skill_for(source: Path) -> str:
    """Skill named by an observation file: the stem before ``@`` (``<skill>@<suffix>.md``)."""
    return source.stem.partition("@")[0]


def index_observations(observations: Path, db: Path) -> IndexResult:
    """Index source Markdown files idempotently, leaving them in place."""
    observations = observations.expanduser().resolve()
    observations.mkdir(parents=True, exist_ok=True)
    result = IndexResult()
    with _connect(db) as conn:
        for source in _files(observations):
            skill = _skill_for(source)
            if not _SKILL_NAME.fullmatch(skill):
                result = IndexResult(result.imported, result.existing, result.skipped + 1)
                continue
            raw = source.read_bytes()
            payload = raw.decode("utf-8")
            oid = observation_id(skill, payload)
            now = _now()
            mtime = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            existing = conn.execute("SELECT id FROM observations WHERE id = ?", (oid,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE observations
                    SET source_path = ?, source_mtime = ?, file_hash = ?, provenance = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(source), mtime, sha256_bytes(raw), _provenance(payload), now, oid),
                )
                result = IndexResult(result.imported, result.existing + 1, result.skipped)
                continue
            conn.execute(
                """
                INSERT INTO observations
                (id, skill, payload, content_hash, file_hash, source_path, source_mtime,
                 provenance, disposition, imported_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (oid, skill, payload, sha256_text(payload), sha256_bytes(raw), str(source), mtime, _provenance(payload), now, now),
            )
            result = IndexResult(result.imported + 1, result.existing, result.skipped)
    return result


def set_disposition(db: Path, record_id: str, disposition: str, *, reason: str | None = None) -> bool:
    """Set one explicit disposition; pending is allowed to reset a prior decision."""
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unsupported disposition: {disposition}")
    with _connect(db) as conn:
        changed = conn.execute(
            """
            UPDATE observations
            SET disposition = ?, disposition_reason = ?, disposition_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (disposition, reason, _now(), _now(), record_id),
        ).rowcount
    return bool(changed)


def _archive_path(observations: Path, row: sqlite3.Row) -> Path:
    stamp = (row["disposition_at"] or _now()).replace(":", "").replace("-", "")[:8]
    return observations / "archive" / row["disposition"] / stamp / f"{row['skill']}-{row['id']}.md"


def archive_dispositioned(observations: Path, db: Path, record_id: str | None = None) -> ArchiveResult:
    """Archive only non-pending source files, preserving their exact payload."""
    observations = observations.expanduser().resolve()
    with _connect(db) as conn:
        query = "SELECT * FROM observations WHERE disposition != 'pending' AND archive_path IS NULL"
        args: tuple[str, ...] = ()
        if record_id:
            query += " AND id = ?"
            args = (record_id,)
        rows = conn.execute(query, args).fetchall()
        pending = int(conn.execute("SELECT COUNT(*) FROM observations WHERE disposition = 'pending'").fetchone()[0])
        result = ArchiveResult(pending=pending)
        for row in rows:
            source = Path(row["source_path"])
            if not source.is_file():
                result = ArchiveResult(result.archived, result.pending, result.missing + 1)
                continue
            raw = source.read_bytes()
            if sha256_bytes(raw) != row["file_hash"] or raw.decode("utf-8") != row["payload"]:
                raise ValueError(f"source changed since indexing: {source}")
            target = _archive_path(observations, row)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            now = _now()
            conn.execute(
                "UPDATE observations SET archive_path = ?, archived_at = ?, updated_at = ? WHERE id = ?",
                (str(target), now, now, row["id"]),
            )
            result = ArchiveResult(result.archived + 1, result.pending, result.missing)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, default=default_observations())
    parser.add_argument("--db", type=Path, default=default_db())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index", help="index top-level observation Markdown files")
    disposition = commands.add_parser("disposition", help="set an explicit disposition for an indexed observation")
    disposition.add_argument("id")
    disposition.add_argument("disposition", choices=DISPOSITIONS)
    disposition.add_argument("--reason")
    archive = commands.add_parser("archive", help="archive dispositioned source files")
    archive.add_argument("--id")
    listing = commands.add_parser("list", help="list indexed observations as JSON")
    listing.add_argument("--disposition", choices=DISPOSITIONS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "index":
        print(json.dumps(index_observations(args.observations, args.db).__dict__, sort_keys=True))
        return 0
    if args.command == "disposition":
        if not set_disposition(args.db, args.id, args.disposition, reason=args.reason):
            parser.error(f"unknown observation id: {args.id}")
        return 0
    if args.command == "archive":
        print(json.dumps(archive_dispositioned(args.observations, args.db, args.id).__dict__, sort_keys=True))
        return 0
    with _connect(args.db) as conn:
        query = "SELECT * FROM observations"
        params: tuple[str, ...] = ()
        if args.disposition:
            query += " WHERE disposition = ?"
            params = (args.disposition,)
        query += " ORDER BY imported_at, id"
        for row in conn.execute(query, params):
            print(json.dumps(dict(row), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
