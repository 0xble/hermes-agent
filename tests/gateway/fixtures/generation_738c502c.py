# Frozen from 738c502c: gateway/generation.py
# Git blob: 0c877b992d8351df3eb614d4c1e5ebfca391e84b
"""Generation identity and coordinator primitives for opt-in overlap handover.

This module is deliberately independent from the legacy singleton startup path.  It
owns only durable identity, generation-scoped paths, and the small coordinator
schema needed by the first handover slice.
"""
from __future__ import annotations

import functools
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from gateway.owned_admission import OwnedAdmissionMixin

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
  heartbeat_at REAL NOT NULL, drain_deadline REAL, suspect_from_state TEXT
);
CREATE TABLE IF NOT EXISTS leases (
  resource TEXT PRIMARY KEY, epoch INTEGER NOT NULL, generation_id TEXT NOT NULL,
  state TEXT NOT NULL, FOREIGN KEY(generation_id) REFERENCES generations(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  profile_home TEXT NOT NULL, transport TEXT NOT NULL, session_key TEXT NOT NULL,
  generation_id TEXT NOT NULL REFERENCES generations(id), epoch INTEGER NOT NULL,
  state TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
  outstanding_work INTEGER NOT NULL DEFAULT 0 CHECK(outstanding_work>=0),
  PRIMARY KEY(profile_home,transport,session_key)
);
CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY, profile_home TEXT NOT NULL, transport TEXT NOT NULL,
  session_key TEXT NOT NULL, source_event_id TEXT NOT NULL, kind TEXT NOT NULL,
  seq INTEGER NOT NULL, owner_id TEXT NOT NULL REFERENCES generations(id),
  owner_epoch INTEGER NOT NULL, authorized_source BLOB NOT NULL,
  payload BLOB NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL DEFAULT 0,
  FOREIGN KEY(profile_home,transport,session_key) REFERENCES sessions(profile_home,transport,session_key),
  UNIQUE(profile_home,transport,source_event_id,kind),
  UNIQUE(profile_home,transport,session_key,seq)
);
CREATE INDEX IF NOT EXISTS inbox_owner_pending ON inbox(owner_id,state,profile_home,transport,session_key,seq);
CREATE TABLE IF NOT EXISTS generation_transfers (
  old_id TEXT NOT NULL REFERENCES generations(id), new_id TEXT NOT NULL REFERENCES generations(id),
  epoch INTEGER NOT NULL, state TEXT NOT NULL, attempt_nonce TEXT NOT NULL,
  PRIMARY KEY(old_id,epoch)
);
CREATE TABLE IF NOT EXISTS transfer_tokens (
  old_id TEXT NOT NULL, epoch INTEGER NOT NULL, token_hash TEXT NOT NULL,
  safe_offset INTEGER, poller_stopped INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(old_id,epoch,token_hash),
  FOREIGN KEY(old_id,epoch) REFERENCES generation_transfers(old_id,epoch)
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


@functools.lru_cache(maxsize=1)
def _boot_id() -> str:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "kern.bootsessionuuid"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            value = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            value = ""
        if value:
            return value
        import psutil
        return f"darwin:{int(psutil.boot_time())}"

    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    import psutil
    return f"boot:{int(psutil.boot_time())}"


class GenerationCoordinator(OwnedAdmissionMixin):
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
        with closing(self.connect()) as conn, conn:
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(generations)")}
            if "suspect_from_state" not in columns:
                conn.execute("ALTER TABLE generations ADD COLUMN suspect_from_state TEXT")
            transfer_columns = {row["name"] for row in conn.execute("PRAGMA table_info(generation_transfers)")}
            if "attempt_nonce" not in transfer_columns:
                conn.execute("ALTER TABLE generation_transfers ADD COLUMN attempt_nonce TEXT")
                conn.execute("UPDATE generation_transfers SET attempt_nonce=? WHERE attempt_nonce IS NULL", (str(uuid.uuid4()),))
            inbox_columns = {row["name"] for row in conn.execute("PRAGMA table_info(inbox)")}
            if "created_at" not in inbox_columns:
                conn.execute("ALTER TABLE inbox ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
            # Old writers omit this additive column. Start their retention window
            # only when a new coordinator sees them, never delete on migration.
            conn.execute("UPDATE inbox SET created_at=? WHERE created_at=0", (time.time(),))
            conn.execute("CREATE INDEX IF NOT EXISTS inbox_retention ON inbox(state,created_at)")
            version = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if version is None:
                conn.execute("INSERT INTO schema_meta(key,value) VALUES('version', ?)",
                             (str(SCHEMA_VERSION),))
            elif int(version["value"]) != SCHEMA_VERSION:
                raise RuntimeError("incompatible gateway coordinator schema")
            conn.commit()

    def register(self, identity: GenerationIdentity, *, state: str = "standby") -> None:
        now = time.time()
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO generations
                (id,release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at,drain_deadline)
                VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
                (identity.id, identity.release_sha, identity.label, identity.pid,
                 identity.started_at, identity.boot_id, identity.start_fingerprint, state, now),
            )
            # Bound terminal history at registration without ever deleting a live or
            # leased generation; remove released lease references before rows.
            terminal = conn.execute(
                "SELECT g.id,g.started_at FROM generations g "
                "WHERE g.state IN ('exited','failed') AND NOT EXISTS "
                "(SELECT 1 FROM leases l WHERE l.generation_id=g.id AND l.state!='released') "
                "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.generation_id=g.id) "
                "AND NOT EXISTS (SELECT 1 FROM inbox i WHERE i.owner_id=g.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_transfers t "
                "JOIN generations peer ON peer.id=CASE WHEN t.old_id=g.id THEN t.new_id ELSE t.old_id END "
                "WHERE (t.old_id=g.id OR t.new_id=g.id) AND peer.state NOT IN ('exited','failed')) "
                "ORDER BY g.started_at DESC,g.id DESC"
            ).fetchall()
            stale = [(row["id"],) for rank, row in enumerate(terminal)
                     if rank >= 20 or row["started_at"] < now - 7 * 86400]
            if stale:
                # Preserve committed transfer evidence for the same bounded history
                # window as its terminal generation. Delete dependent receipts first.
                # Row-value IN requires SQLite 3.15 or later.
                conn.executemany("DELETE FROM transfer_tokens WHERE old_id=? OR (old_id,epoch) IN "
                                 "(SELECT old_id,epoch FROM generation_transfers WHERE new_id=?)",
                                 [(row[0], row[0]) for row in stale])
                conn.executemany("DELETE FROM generation_transfers WHERE old_id=? OR new_id=?",
                                 [(row[0], row[0]) for row in stale])
                conn.executemany("DELETE FROM leases WHERE generation_id=? AND state='released'", stale)
                conn.executemany("DELETE FROM generations WHERE id=?", stale)
            conn.commit()

    def heartbeat(self, generation_id: str, *, state: str | None = None) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if state is None:
                conn.execute(
                    "UPDATE generations SET heartbeat_at=?, "
                    "state=CASE WHEN state='suspect' THEN COALESCE(suspect_from_state,'ready') ELSE state END, "
                    "suspect_from_state=NULL WHERE id=?", (time.time(), generation_id))
            else:
                conn.execute("UPDATE generations SET heartbeat_at=?,state=?,suspect_from_state=NULL WHERE id=?",
                             (time.time(), state, generation_id))
            conn.commit()

    def acquire_lease(self, resource: str, generation_id: str, *, state: str = "active") -> int:
        with closing(self.connect()) as conn, conn:
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
                        conn.execute(
                            "UPDATE generations SET suspect_from_state=CASE WHEN state='suspect' "
                            "THEN suspect_from_state ELSE state END,state='suspect' WHERE id=?",
                            (row["generation_id"],))
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

    def request_transfer(self, old_id: str, new_id: str, epoch: int,
                         tokens: set[str]) -> None:
        """Freeze the expected token roster before asking the old process to stop."""
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            successor = conn.execute("SELECT state FROM generations WHERE id=?", (new_id,)).fetchone()
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (old_id, epoch, "active"):
                raise RuntimeError("active generation lease changed; transfer refused")
            if not successor or successor["state"] != "ready" or old_id == new_id:
                raise RuntimeError("successor is not ready")
            existing = conn.execute(
                "SELECT new_id,state FROM generation_transfers WHERE old_id=? AND epoch=?",
                (old_id, epoch)).fetchone()
            if existing and existing["state"] != "aborted":
                raise RuntimeError("transfer already requested")
            attempt_nonce = str(uuid.uuid4())
            if existing:
                conn.execute("DELETE FROM transfer_tokens WHERE old_id=? AND epoch=?", (old_id, epoch))
                conn.execute("UPDATE generation_transfers SET new_id=?,state='requested',attempt_nonce=? "
                             "WHERE old_id=? AND epoch=?", (new_id, attempt_nonce, old_id, epoch))
            else:
                conn.execute("INSERT INTO generation_transfers VALUES (?,?,?,'requested',?)",
                             (old_id, new_id, epoch, attempt_nonce))
            conn.executemany("INSERT INTO transfer_tokens(old_id,epoch,token_hash) VALUES(?,?,?)",
                             [(old_id, epoch, token) for token in sorted(tokens)])
            conn.commit()

    def transfer_attempt_nonce(self, old_id: str, epoch: int) -> str:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT attempt_nonce FROM generation_transfers WHERE old_id=? AND epoch=?",
                               (old_id, epoch)).fetchone()
        if not row or not row["attempt_nonce"]:
            raise RuntimeError("transfer attempt is missing")
        return row["attempt_nonce"]

    def record_poller_stopped(self, old_id: str, epoch: int, token_hash: str,
                              safe_offset: int, *, attempt_nonce: str | None = None) -> None:
        """Persist only a receipt for a token in the frozen roster and current lease."""
        if type(safe_offset) is not int or safe_offset < 0:
            raise RuntimeError("invalid polling cursor")
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            transfer = conn.execute("SELECT state,attempt_nonce FROM generation_transfers WHERE old_id=? AND epoch=?",
                                    (old_id, epoch)).fetchone()
            if (not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (old_id, epoch, "active")
                    or not transfer or transfer["state"] != "requested"
                    or (attempt_nonce is not None and transfer["attempt_nonce"] != attempt_nonce)):
                raise RuntimeError("poller stop receipt is not for the current transfer attempt")
            changed = conn.execute("UPDATE transfer_tokens SET safe_offset=?,poller_stopped=1 "
                                   "WHERE old_id=? AND epoch=? AND token_hash=? AND poller_stopped=0",
                                   (safe_offset, old_id, epoch, token_hash)).rowcount
            if not changed:
                raise RuntimeError("token is not pending a stop receipt")
            conn.commit()

    def transfer_receipts(self, old_id: str, epoch: int) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn, conn:
            return [dict(row) for row in conn.execute(
                "SELECT token_hash,safe_offset,poller_stopped FROM transfer_tokens WHERE old_id=? AND epoch=? ORDER BY token_hash",
                (old_id, epoch)).fetchall()]

    def commit_transfer(self, old_id: str, new_id: str, epoch: int,
                        *, drain_seconds: float = 7200) -> int:
        """CAS promotion; a live old holder never loses its lease without its receipts."""
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT new_id,state FROM generation_transfers WHERE old_id=? AND epoch=?",
                                    (old_id, epoch)).fetchone()
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            successor = conn.execute("SELECT state FROM generations WHERE id=?", (new_id,)).fetchone()
            if not transfer or transfer["new_id"] != new_id or transfer["state"] != "requested" or not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (old_id, epoch, "active"):
                raise RuntimeError("transfer request or lease changed")
            if not successor or successor["state"] != "ready":
                raise RuntimeError("successor is not ready")
            if conn.execute("SELECT 1 FROM transfer_tokens WHERE old_id=? AND epoch=? AND poller_stopped=0 LIMIT 1",
                            (old_id, epoch)).fetchone():
                raise RuntimeError("missing poller stop receipt")
            changed = conn.execute("UPDATE leases SET generation_id=?,epoch=?,state='active' "
                                   "WHERE resource='active_generation' AND generation_id=? AND epoch=? AND state='active'",
                                   (new_id, epoch + 1, old_id, epoch)).rowcount
            if changed != 1:
                raise RuntimeError("transfer lease compare-and-swap failed")
            conn.execute("UPDATE generations SET state='draining',drain_deadline=? WHERE id=?",
                         (time.time() + drain_seconds, old_id))
            conn.execute("UPDATE generations SET state='serving' WHERE id=?", (new_id,))
            conn.execute("UPDATE generation_transfers SET state='committed' WHERE old_id=? AND epoch=?",
                         (old_id, epoch))
            conn.commit()
            return epoch + 1

    def abort_transfer(self, old_id: str, new_id: str, epoch: int,
                       *, attempt_nonce: str) -> bool:
        """CAS abort against the old lease and exact attempt, never a committed successor."""
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (old_id, epoch, "active"):
                raise RuntimeError("cannot abort a committed transfer")
            changed = conn.execute(
                "UPDATE generation_transfers SET state='aborted' WHERE old_id=? AND new_id=? "
                "AND epoch=? AND state='requested' AND attempt_nonce=?",
                (old_id, new_id, epoch, attempt_nonce)).rowcount
            conn.commit()
            return changed == 1

    def project_active_summary(self, identity: GenerationIdentity, epoch: int,
                               runtime: dict[str, Any]) -> bool:
        """Project the lease holder to legacy files under the SQLite write fence.

        ``gateway.pid`` is the one sanctioned bypass of generation-scoped files:
        legacy clients need B's PID after transfer, so A's exit must never
        unlink this projection. Only the fenced lease holder may overwrite it.
        """
        from gateway.status import _build_pid_record, _clear_running_pid_cache
        # Keep both projections inside the write transaction: committing the lease check before
        # these writes would let a successor acquire the lease and then be overwritten by this
        # stale writer before the legacy files are updated.
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            if not row or (row["generation_id"], row["epoch"], row["state"]) != (identity.id, epoch, "active"):
                return False
            write_generation_record(self.home / "gateway_state.json", identity,
                                    state="serving", runtime=runtime)
            write_generation_record(self.home / "gateway.pid", identity,
                                    state="serving", runtime=_build_pid_record())
            _clear_running_pid_cache()
            conn.commit()
            return True

    def fence_draining_generation(self, generation_id: str) -> int:
        """At the hard cap, retain owner and payload evidence without replay eligibility."""
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state,drain_deadline FROM generations WHERE id=?", (generation_id,)).fetchone()
            if not row or row["state"] != "draining" or (row["drain_deadline"] is not None and time.time() < row["drain_deadline"]):
                raise RuntimeError("generation has not reached its drain cap")
            changed = conn.execute("UPDATE sessions SET state='interrupted' WHERE generation_id=? AND state='owned'",
                                   (generation_id,)).rowcount
            conn.execute("UPDATE inbox SET state='interrupted' WHERE owner_id=? AND state='pending'", (generation_id,))
            conn.commit()
            return changed

    def project_stopped_summary(self, identity: GenerationIdentity, epoch: int) -> bool:
        """Clear the compatibility snapshot only while holding the exact active lease."""
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (identity.id, epoch, "active"):
                return False
            from gateway.status import read_runtime_status
            path = self.home / "gateway_state.json"
            runtime = read_runtime_status(path) or {}
            write_generation_record(path, identity, state="exited",
                                    runtime={**runtime, "gateway_state": "stopped"}, clear_pid=True)
            conn.commit()
            return True

    def release_lease(self, resource: str, generation_id: str, epoch: int) -> bool:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE leases SET state='released' WHERE resource=? AND generation_id=? "
                "AND epoch=? AND state!='released'",
                (resource, generation_id, epoch),
            ).rowcount
            conn.commit()
            return bool(changed)

    def generations(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn, conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM generations ORDER BY started_at, id").fetchall()]

    def leases(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn, conn:
            return [dict(row) for row in conn.execute("SELECT * FROM leases ORDER BY resource").fetchall()]


def generation_paths(home: Path, identity: GenerationIdentity) -> dict[str, Path]:
    """Return generation-specific identity files; a long UNIX socket path uses a short scratch endpoint."""
    root = Path(home)
    suffix = identity.id
    socket = root / f"gateway.{suffix}.sock"
    if len(os.fsencode(socket)) >= 100:
        import hashlib
        digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
        socket = Path(os.path.sep, "tmp", f"hg-{getattr(os, 'getuid', lambda: 0)()}-{digest}") / f"{suffix[:32]}.sock"
    return {
        "pid": root / f"gateway.{suffix}.pid",
        "socket": socket,
        "host": root / f"gateway.{suffix}.host.json",
        "state": root / f"gateway_state.{suffix}.json",
    }


def write_generation_record(path: Path, identity: GenerationIdentity, *, state: str = "standby",
                            socket_path: Path | None = None,
                            runtime: dict[str, Any] | None = None, clear_pid: bool = False) -> None:
    payload = dict(runtime or {})
    payload.update(identity.as_record(state=state))
    if clear_pid:
        payload["pid"] = None
    if socket_path is not None:
        payload["socket_path"] = str(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Heartbeat and status writers can race within one process. A PID-only name lets
    # one os.replace consume the other's temporary file, crashing promotion.
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
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
        value = ((config.get("gateway") or {}).get("overlap_handover") or {}).get("enabled", False)
    else:
        value = getattr(config, "overlap_handover_enabled", False)
    return bool(value)


__all__ = [
    "GenerationCoordinator", "GenerationIdentity", "SCHEMA_VERSION",
    "generation_paths", "overlap_handover_enabled", "remove_generation_files",
    "write_generation_record",
]
