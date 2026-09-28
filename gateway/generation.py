"""Generation identity and coordinator primitives for opt-in overlap handover.

This module is deliberately independent from the legacy singleton startup path.  It
owns only durable identity, generation-scoped paths, and the small coordinator
schema needed by the first handover slice.
"""
from __future__ import annotations

import json
import os
import platform
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS generations (
  id TEXT PRIMARY KEY, release_sha TEXT NOT NULL, label TEXT NOT NULL,
  pid INTEGER NOT NULL, started_at REAL NOT NULL, boot_id TEXT NOT NULL,
  start_fingerprint TEXT NOT NULL, state TEXT NOT NULL,
  heartbeat_at REAL NOT NULL, drain_deadline REAL
);
CREATE TABLE IF NOT EXISTS leases (
  resource TEXT PRIMARY KEY, epoch INTEGER NOT NULL, generation_id TEXT NOT NULL,
  state TEXT NOT NULL, FOREIGN KEY(generation_id) REFERENCES generations(id)
);
"""


@dataclass(frozen=True)
class GenerationIdentity:
    id: str
    release_sha: str
    label: str
    pid: int
    started_at: float
    boot_id: str
    start_fingerprint: str

    @classmethod
    def create(cls, *, release_sha: str, label: str, pid: int | None = None,
               boot_id: str | None = None, started_at: float | None = None,
               start_fingerprint: str | None = None) -> "GenerationIdentity":
        started = time.time() if started_at is None else float(started_at)
        process_id = os.getpid() if pid is None else int(pid)
        fingerprint = start_fingerprint or f"{process_id}:{started:.6f}"
        return cls(
            id=str(uuid.uuid4()), release_sha=str(release_sha), label=str(label),
            pid=process_id, started_at=started,
            boot_id=boot_id or _boot_id(), start_fingerprint=fingerprint,
        )

    def as_record(self, *, state: str = "standby", heartbeat_at: float | None = None) -> dict[str, Any]:
        record = asdict(self)
        record.update(state=state, heartbeat_at=time.time() if heartbeat_at is None else heartbeat_at,
                      drain_deadline=None)
        return record


def _boot_id() -> str:
    for path in (Path("/var/run/boot_id"), Path("/proc/sys/kernel/random/boot_id")):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    import psutil
    return f"{platform.node()}:{psutil.boot_time():.6f}"


class GenerationCoordinator:
    """SQLite coordinator with explicit transaction boundaries for generation records."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / "gateway-coordinator.db"
        self.home.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if version is None:
                conn.execute("INSERT INTO schema_meta(key,value) VALUES('version', ?)",
                             (str(SCHEMA_VERSION),))
            elif int(version["value"]) != SCHEMA_VERSION:
                raise RuntimeError("incompatible gateway coordinator schema")
            conn.commit()

    def register(self, identity: GenerationIdentity, *, state: str = "standby") -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO generations
                (id,release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at,drain_deadline)
                VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
                (identity.id, identity.release_sha, identity.label, identity.pid,
                 identity.started_at, identity.boot_id, identity.start_fingerprint, state, now),
            )
            conn.commit()

    def heartbeat(self, generation_id: str, *, state: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if state is None:
                conn.execute("UPDATE generations SET heartbeat_at=? WHERE id=?", (time.time(), generation_id))
            else:
                conn.execute("UPDATE generations SET heartbeat_at=?,state=? WHERE id=?",
                             (time.time(), state, generation_id))
            conn.commit()

    def acquire_lease(self, resource: str, generation_id: str, *, state: str = "active") -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT epoch,generation_id,state FROM leases WHERE resource=?", (resource,)).fetchone()
            if row and row["state"] != "released" and row["generation_id"] != generation_id:
                from gateway.status import _get_process_start_time, _pid_exists
                holder = conn.execute(
                    "SELECT pid,boot_id,start_fingerprint,heartbeat_at FROM generations WHERE id=?",
                    (row["generation_id"],),
                ).fetchone()
                if holder is None:
                    raise RuntimeError(f"lease {resource!r} has no generation record; takeover refused")
                pid = int(holder["pid"])
                # Unknown process start is not death proof. Fail closed while the PID lives.
                alive = _pid_exists(pid)
                actual_start = _get_process_start_time(pid) if alive else None
                dead = (holder["boot_id"] != _boot_id() or not alive or
                        (actual_start is not None and holder["start_fingerprint"] != f"{pid}:{actual_start}"))
                if dead:
                    conn.execute("UPDATE generations SET state='failed' WHERE id=?", (row["generation_id"],))
                else:
                    if time.time() - holder["heartbeat_at"] > 5:
                        conn.execute("UPDATE generations SET state='suspect' WHERE id=?", (row["generation_id"],))
                        conn.commit()
                        raise RuntimeError(f"lease {resource!r} is suspect: holder PID {pid} is alive; takeover refused")
                    raise RuntimeError(f"lease {resource!r} is held by another generation")
            epoch = (int(row["epoch"]) + 1) if row else 1
            conn.execute(
                "INSERT OR REPLACE INTO leases(resource,epoch,generation_id,state) VALUES(?,?,?,?)",
                (resource, epoch, generation_id, state),
            )
            conn.commit()
            return epoch

    def release_lease(self, resource: str, generation_id: str, epoch: int) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE leases SET state='released' WHERE resource=? AND generation_id=? "
                "AND epoch=? AND state!='released'",
                (resource, generation_id, epoch),
            ).rowcount
            conn.commit()
            return bool(changed)

    def generations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM generations ORDER BY started_at, id").fetchall()]

    def leases(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM leases ORDER BY resource").fetchall()]


def generation_paths(home: Path, identity: GenerationIdentity) -> dict[str, Path]:
    """Return generation-specific identity files; a long UNIX socket path uses a short scratch endpoint."""
    root = Path(home)
    suffix = identity.id
    socket = root / f"gateway.{suffix}.sock"
    if len(os.fsencode(socket)) >= 100:
        import hashlib
        scratch = Path(os.getenv("TMPDIR", str(root)))
        socket = scratch / f"hg-{hashlib.sha256(os.fsencode(root / suffix)).hexdigest()[:16]}.sock"
    return {
        "pid": root / f"gateway.{suffix}.pid",
        "socket": socket,
        "host": root / f"gateway.{suffix}.host.json",
        "state": root / f"gateway_state.{suffix}.json",
    }


def write_generation_record(path: Path, identity: GenerationIdentity, *, state: str = "standby",
                            socket_path: Path | None = None,
                            runtime: dict[str, Any] | None = None) -> None:
    payload = dict(runtime or {})
    payload.update(identity.as_record(state=state))
    if socket_path is not None:
        payload["socket_path"] = str(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def remove_generation_files(home: Path, identity: GenerationIdentity) -> None:
    """Remove only records whose identity and start fingerprint match this generation."""
    for name, path in generation_paths(home, identity).items():
        if name == "socket":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("id") != identity.id or payload.get("start_fingerprint") != identity.start_fingerprint:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def overlap_handover_enabled(config: Any) -> bool:
    value = config
    if isinstance(config, dict):
        value = (config.get("gateway") or {}).get("overlap_handover", {}).get("enabled", False)
    else:
        value = getattr(config, "overlap_handover_enabled", False)
    return bool(value)


__all__ = [
    "GenerationCoordinator", "GenerationIdentity", "SCHEMA_VERSION",
    "generation_paths", "overlap_handover_enabled", "remove_generation_files",
    "write_generation_record",
]
