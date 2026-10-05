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
from typing import Any, Callable, Iterable

from gateway.owned_admission import OwnedAdmissionMixin
from gateway.generation_claims import GenerationClaimsMixin
from gateway.generation_retention import GenerationRetentionMixin, install_retention_fences
from gateway.deadline import (
    begin_immediate,
    check as check_deadline,
    connect_sqlite,
    remaining as deadline_remaining,
    with_deadline_scope,
)
from gateway import deadline as gateway_deadline

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS generations (
  id TEXT PRIMARY KEY, release_sha TEXT NOT NULL, label TEXT NOT NULL,
  pid INTEGER, started_at REAL NOT NULL, boot_id TEXT NOT NULL,
  start_fingerprint TEXT, state TEXT NOT NULL,
  heartbeat_at REAL NOT NULL, drain_deadline REAL, suspect_from_state TEXT,
  verdict TEXT, verdict_at REAL, verdict_evidence TEXT,
  suspect_at REAL, claim_pending INTEGER NOT NULL DEFAULT 0,
  claim_boot_id TEXT, claim_at REAL, scope_nonce TEXT
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
CREATE TABLE IF NOT EXISTS poller_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  generation_id TEXT NOT NULL REFERENCES generations(id),
  epoch INTEGER NOT NULL,
  token_hash TEXT NOT NULL,
  event TEXT NOT NULL,
  monotonic_at REAL NOT NULL,
  wall_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS poller_journal_token ON poller_journal(token_hash,id);
CREATE TABLE IF NOT EXISTS lease_moves (
  resource TEXT NOT NULL, old_id TEXT NOT NULL, new_id TEXT NOT NULL,
  old_epoch INTEGER NOT NULL, new_epoch INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('handover','takeover')),
  PRIMARY KEY(resource,new_epoch)
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


RUNTIME_STATES = frozenset({"standby", "serving", "draining", "exited"})
_STATE_TRANSITIONS = {
    "standby": frozenset({"serving", "exited"}),
    "serving": frozenset({"draining", "exited"}),
    "draining": frozenset({"exited"}),
    "exited": frozenset(),
}
_POLL_JOURNAL_EVENTS = frozenset({
    "lock_acquired", "lock_released", "poller_started", "poller_stopped",
})


def generation_start_fingerprint_matches(row, observed) -> bool | None:
    """Reconcile a same-host reading; an unavailable reading never proves death."""
    from gateway.status import start_time_fingerprints_match
    if observed is None or not row['start_fingerprint']:
        return None
    try:
        pid, recorded = row['start_fingerprint'].split(':', 1)
        return int(pid) == int(row['pid']) and start_time_fingerprints_match(float(recorded), observed)
    except (TypeError, ValueError, OverflowError):
        return False  # A nonnumeric legacy fingerprint cannot match this reading.


def _is_unclaimed(row: sqlite3.Row | dict[str, Any]) -> bool:
    return bool(row.get("claim_pending", 0) if isinstance(row, dict) else row["claim_pending"]) \
        or ((row.get("pid") if isinstance(row, dict) else row["pid"]) is None) \
        or ((row.get("pid") if isinstance(row, dict) else row["pid"]) == 0 and
            (row.get("start_fingerprint") if isinstance(row, dict) else row["start_fingerprint"]) == "")


def check_poller_journal(rows: Iterable[dict[str, Any] | sqlite3.Row]) -> dict[str, Any]:
    """Closed, well-formed evidence is required for a clean offline verdict.

    Wall time spans boots. Monotonic time must be ordered within a boot, but
    cannot be subtracted across boots. Unclosed crash intervals stay unknown.
    """
    import math
    ordered = []
    malformed = False
    for row in rows:
        try:
            ordered.append(dict(row))
        except (TypeError, ValueError):
            malformed = True
    locks, pollers, last_stop = {}, {}, {}
    violations, gaps = [], []
    previous = {}
    seen_pollers = set()
    for row in ordered:
        token, event = row.get("token_hash"), row.get("event")
        owner = (row.get("generation_id"), row.get("epoch"))
        wall, mono = row.get("wall_at"), row.get("monotonic_at")
        if (not isinstance(token, str) or not token or not isinstance(event, str)
                or event not in _POLL_JOURNAL_EVENTS or not isinstance(owner[0], str)
                or not owner[0] or type(owner[1]) is not int or owner[1] < 1
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in (wall, mono))):
            violations.append({"row": row, "reason": "invalid_event"})
            continue
        boot = row.get("boot_id")
        prior = previous.get(token)
        if prior and (wall < prior[0] or (boot and boot == prior[2] and mono < prior[1])):
            violations.append({"row": row, "reason": "clock_regression"})
        previous[token] = (wall, mono, boot)
        if event == "lock_acquired":
            if token in locks:
                violations.append({"row": row, "reason": "overlapping_lock"})
            locks[token] = owner
        elif event == "lock_released":
            if locks.get(token) != owner or token in pollers:
                violations.append({"row": row, "reason": "lock_release_mismatch"})
            locks.pop(token, None)
        elif event == "poller_started":
            seen_pollers.add(token)
            if token in pollers:
                violations.append({"row": row, "reason": "overlapping_poller"})
            if locks.get(token) != owner:
                violations.append({"row": row, "reason": "poller_without_lock"})
            if token in last_stop:
                stopped_wall, stopped_mono, stopped_boot = last_stop[token]
                elapsed = mono - stopped_mono if boot and boot == stopped_boot else wall - stopped_wall
                gaps.append(max(0.0, elapsed))
            pollers[token] = owner
        else:
            if pollers.get(token) != owner:
                violations.append({"row": row, "reason": "poller_stop_mismatch"})
            pollers.pop(token, None)
            last_stop[token] = (wall, mono, boot)
    open_intervals = [{"token_hash": token, "kind": kind,
                       "generation_id": owner[0], "epoch": owner[1]}
                      for kind, active in (("lock", locks), ("poller", pollers))
                      for token, owner in active.items()]
    if not ordered:
        violations.append({"reason": "missing_evidence"})
    if malformed:
        violations.append({"reason": "invalid_event"})
    if set(previous) - seen_pollers:
        violations.append({"reason": "missing_poller_evidence"})
    if open_intervals:
        violations.append({"reason": "unclosed_intervals"})
    return {"ok": not violations, "violations": violations,
            "open_intervals": open_intervals,
            "longest_zero_poller_gap": max(gaps, default=0.0),
            "event_count": len(ordered)}


class GenerationCoordinator(GenerationClaimsMixin, GenerationRetentionMixin, OwnedAdmissionMixin):
    """SQLite coordinator with explicit transaction boundaries for generation records."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / "gateway-coordinator.db"
        self.home.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self, *, timeout: float = 5.0) -> sqlite3.Connection:
        budget = deadline_remaining()
        effective_timeout = timeout if budget is None else min(float(timeout), budget)
        if effective_timeout <= 0:
            raise TimeoutError("gateway deadline exceeded")
        conn = connect_sqlite(self.path, timeout=effective_timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={max(0, int(effective_timeout * 1000))}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _deadline_connect(self, deadline: float | None):
        if deadline is None:
            return self.connect()
        budget = deadline - gateway_deadline.now()
        if budget <= 0:
            raise TimeoutError("generation transaction deadline exceeded")
        return self.connect(timeout=min(5.0, budget))

    @staticmethod
    def _check_transaction_deadline(conn, deadline: float | None = None):
        try:
            if deadline is not None and gateway_deadline.now() >= deadline:
                conn.rollback()
                raise TimeoutError("generation transaction deadline exceeded")
            check_deadline()
        except TimeoutError:
            conn.rollback()
            raise

    @staticmethod
    def _begin_immediate(conn, deadline: float | None = None):
        if deadline is None or deadline_remaining() is not None:
            begin_immediate(conn)
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if gateway_deadline.now() >= deadline:
                conn.rollback()
                raise TimeoutError("generation transaction deadline exceeded") from exc
            raise
        GenerationCoordinator._check_transaction_deadline(conn, deadline)

    def _initialize(self) -> None:
        from gateway.generation_schema import layout_is_current, record_current_layout
        try:
            retention_boot = _boot_id()
        except OSError:
            retention_boot = None  # Without a boot identity, retain all history.
        with closing(self.connect()) as conn, conn:
            if layout_is_current(conn, retention_boot):
                return
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA foreign_keys=OFF")
            begin_immediate(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(generations)")}
            additive_columns = {
                "verdict": "TEXT",
                "verdict_at": "REAL",
                "verdict_evidence": "TEXT",
                "suspect_at": "REAL",
                "suspect_evidence": "TEXT",
                "claim_pending": "INTEGER NOT NULL DEFAULT 0",
                "claim_boot_id": "TEXT",
                "claim_at": "REAL",
                "legacy_ready_at": "REAL",
                "scope_nonce": "TEXT",
            }
            for name, declaration in additive_columns.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE generations ADD COLUMN {name} {declaration}")
            journal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(poller_journal)")}
            if "boot_id" not in journal_columns:
                conn.execute("ALTER TABLE poller_journal ADD COLUMN boot_id TEXT")
            # N-1 opens and writes this upgraded DB with the legacy key off, or
            # in legacy overlap until forward-only has run on this profile. Its
            # legacy-key-on path after that is outside the design contract
            # (seamless-restart.md, Flag and previous release). Keep version 1
            # and use additive objects/columns for the supported configurations.
            if "suspect_from_state" not in columns:
                conn.execute("ALTER TABLE generations ADD COLUMN suspect_from_state TEXT")
            from gateway.generation_schema import upgrade_claim_schema
            upgrade_claim_schema(conn)
            conn.execute("UPDATE generations SET verdict=COALESCE(verdict,'failed'), "
                         "verdict_at=COALESCE(verdict_at,heartbeat_at), "
                         "verdict_evidence=COALESCE(verdict_evidence,'legacy_state_failed'),state='exited' "
                         "WHERE state='failed'")
            conn.execute("UPDATE generations SET suspect_at=heartbeat_at, "
                         "state=CASE WHEN suspect_from_state IN ('serving','draining') "
                         "THEN suspect_from_state WHEN EXISTS(SELECT 1 FROM leases "
                         "WHERE generation_id=generations.id AND resource='active_generation' AND state='active') "
                         "THEN 'serving' ELSE 'standby' END WHERE state='suspect'")
            # N-1 'ready' described both the serving holder and a passive standby.
            # Resolve that ambiguity from durable authority, never heartbeat age.
            conn.execute("UPDATE generations SET state=CASE WHEN EXISTS(SELECT 1 FROM leases "
                         "WHERE generation_id=generations.id AND resource='active_generation' AND state='active') "
                         "THEN 'serving' ELSE 'standby' END WHERE state IN ('ready','starting')")
            if conn.execute("SELECT 1 FROM generations WHERE state NOT IN "
                            "('standby','serving','draining','exited') LIMIT 1").fetchone():
                raise RuntimeError("unknown legacy generation runtime state")
            conn.execute("CREATE INDEX IF NOT EXISTS generations_scope "
                         "ON generations(label,boot_id,scope_nonce)")
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
            conn.execute("CREATE TRIGGER IF NOT EXISTS permanent_generation_verdict BEFORE UPDATE ON generations "
                         "WHEN OLD.verdict IS NOT NULL AND (NEW.verdict IS NOT OLD.verdict OR "
                         "NEW.verdict_at IS NOT OLD.verdict_at OR NEW.verdict_evidence IS NOT OLD.verdict_evidence) "
                         "BEGIN SELECT RAISE(ABORT,'generation verdict is permanent'); END")
            conn.execute("CREATE TRIGGER IF NOT EXISTS generation_runtime_insert BEFORE INSERT ON generations "
                         "WHEN NEW.state NOT IN ('standby','serving','draining','exited') "
                         "BEGIN SELECT RAISE(ABORT,'invalid generation runtime state'); END")
            conn.execute("CREATE TRIGGER IF NOT EXISTS lease_move_authority BEFORE UPDATE ON leases "
                         "WHEN (NEW.generation_id != OLD.generation_id OR NEW.epoch != OLD.epoch) AND NOT EXISTS "
                         "(SELECT 1 FROM lease_moves WHERE resource=OLD.resource AND old_id=OLD.generation_id "
                         "AND new_id=NEW.generation_id AND old_epoch=OLD.epoch AND new_epoch=NEW.epoch "
                         "AND new_epoch=old_epoch+1) "
                         "BEGIN SELECT RAISE(ABORT,'lease move requires handover or takeover'); END")
            conn.execute("CREATE TRIGGER IF NOT EXISTS lease_claim_required BEFORE INSERT ON leases "
                         "WHEN NOT EXISTS(SELECT 1 FROM generations WHERE id=NEW.generation_id "
                         "AND claim_pending=0 AND pid>0 AND verdict IS NULL "
                         "AND state IN ('standby','serving')) "
                         "BEGIN SELECT RAISE(ABORT,'lease requires a claimed live generation'); END")
            # N-1 acquire_lease uses INSERT OR REPLACE, which skips UPDATE
            # triggers and may skip DELETE triggers. Fence that writer too.
            conn.execute("CREATE TRIGGER IF NOT EXISTS lease_replace_authority BEFORE INSERT ON leases "
                         "WHEN EXISTS(SELECT 1 FROM leases WHERE resource=NEW.resource) "
                         "AND NOT EXISTS(SELECT 1 FROM leases l JOIN lease_moves m ON m.resource=l.resource "
                         "WHERE l.resource=NEW.resource AND m.old_id=l.generation_id AND m.old_epoch=l.epoch "
                         "AND m.new_id=NEW.generation_id AND m.new_epoch=NEW.epoch AND m.new_epoch=m.old_epoch+1) "
                         "BEGIN SELECT RAISE(ABORT,'lease move requires handover or takeover'); END")
            conn.execute("CREATE TRIGGER IF NOT EXISTS lease_retain_epoch BEFORE DELETE ON leases "
                         "BEGIN SELECT RAISE(IGNORE); END")
            # Translate N-1 publications inside its transaction. Neither legacy
            # spelling can restore runtime authority or move a lease.
            conn.execute("CREATE TRIGGER IF NOT EXISTS generation_legacy_failed BEFORE UPDATE OF state ON generations "
                         "WHEN NEW.state='failed' BEGIN "
                         "UPDATE generations SET state='exited',heartbeat_at=NEW.heartbeat_at, "
                         "verdict=COALESCE(verdict,'failed'),verdict_at=COALESCE(verdict_at,(julianday('now')-2440587.5)*86400.0), "
                         "verdict_evidence=COALESCE(verdict_evidence,'legacy_state_failed') WHERE id=OLD.id; "
                         "SELECT RAISE(IGNORE); END")
            # Replace earlier trigger definitions after additive migration. Ready
            # publication only refreshes evidence, not legacy suspicion metadata.
            conn.execute("DROP TRIGGER IF EXISTS generation_legacy_ready")
            conn.execute("CREATE TRIGGER generation_legacy_ready BEFORE UPDATE OF state ON generations "
                         "WHEN NEW.state='ready' BEGIN "
                         "UPDATE generations SET heartbeat_at=NEW.heartbeat_at,legacy_ready_at=NEW.heartbeat_at WHERE id=OLD.id; "
                         "SELECT RAISE(IGNORE); END")
            conn.execute("DROP TRIGGER IF EXISTS generation_forward_only")
            conn.execute("CREATE TRIGGER IF NOT EXISTS generation_forward_only BEFORE UPDATE OF state ON generations "
                         "WHEN NEW.state NOT IN ('failed','ready') AND (NEW.state NOT IN ('standby','serving','draining','exited') OR "
                         "(NEW.state != OLD.state AND NOT "
                         "((OLD.state='standby' AND NEW.state IN ('serving','exited')) OR "
                         "(OLD.state='serving' AND NEW.state IN ('draining','exited')) OR "
                         "(OLD.state='draining' AND NEW.state='exited')))) "
                         "BEGIN SELECT RAISE(ABORT,'generation runtime is forward-only'); END")
            conn.execute("CREATE TRIGGER IF NOT EXISTS generation_claim_immutable BEFORE UPDATE ON generations "
                         "WHEN NEW.id != OLD.id OR NEW.label != OLD.label OR NEW.release_sha != OLD.release_sha OR "
                         "(OLD.pid>0 AND (NEW.pid IS NOT OLD.pid OR NEW.start_fingerprint IS NOT OLD.start_fingerprint "
                         "OR NEW.boot_id != OLD.boot_id OR NEW.scope_nonce IS NOT OLD.scope_nonce)) "
                         "BEGIN SELECT RAISE(ABORT,'generation identity is immutable'); END")
            for table in ("poller_journal", "lease_moves"):
                for action in (("UPDATE",) if table == "poller_journal" else ("UPDATE", "DELETE")):
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} "
                                 f"ON {table} BEGIN SELECT RAISE(ABORT,'authority journal is append-only'); END")
            install_retention_fences(conn, retention_boot)
            record_current_layout(conn, retention_boot)
            conn.commit()

    def register(self, identity: GenerationIdentity, *, state: str = "standby") -> None:
        """Register an already identified process for legacy-compatible callers.

        New updater paths should call :meth:`reserve_generation` followed by the
        one-shot :meth:`claim_generation`; this method remains claimed by default
        so the flag-off singleton path is unchanged.
        """
        if state not in RUNTIME_STATES:
            raise ValueError(f"invalid generation runtime state: {state}")
        now = time.time()
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            self._register_in_transaction(conn, identity, state=state, heartbeat_at=now)
            conn.commit()

    @staticmethod
    def _register_in_transaction(conn, identity, *, state="standby", heartbeat_at=None):
        conn.execute(
            """INSERT INTO generations
            (id,release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at,drain_deadline,claim_pending)
            VALUES (?,?,?,?,?,?,?,?,?,NULL,0)""",
            (identity.id, identity.release_sha, identity.label, identity.pid,
             identity.started_at, identity.boot_id, identity.start_fingerprint, state,
             time.time() if heartbeat_at is None else heartbeat_at),
        )

    def reserve_generation(self, *, release_sha: str, label: str,
                           generation_id: str | None = None,
                           boot_id: str | None = None,
                           started_at: float | None = None) -> GenerationIdentity:
        """Insert an unclaimed generation before any supervisor action.

        NULL PID and fingerprint distinguish a reservation from its first process.
        """
        identity = GenerationIdentity(
            id=generation_id or str(uuid.uuid4()), release_sha=str(release_sha),
            label=str(label), pid=0,
            started_at=time.time() if started_at is None else float(started_at),
            boot_id=boot_id or _boot_id(), start_fingerprint="",
        )
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            self._ensure_live_label_index(conn)
            self._reserve_in_transaction(conn, identity)
            conn.commit()
        return identity

    @staticmethod
    def _reserve_in_transaction(conn, identity):
        conn.execute("""INSERT INTO generations
            (id,release_sha,label,pid,started_at,boot_id,start_fingerprint,state,heartbeat_at,claim_pending)
            VALUES (?,?,?,NULL,?,?,NULL,'standby',?,1)""",
            (identity.id, identity.release_sha, identity.label, identity.started_at, identity.boot_id, time.time()))

    def register_unclaimed(self, identity: GenerationIdentity) -> None:
        """Compatibility spelling for reserving a caller-created generation identity."""
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            self._ensure_live_label_index(conn)
            self._reserve_in_transaction(conn, identity)
            conn.commit()

    @staticmethod
    def _claim_in_transaction(conn, generation_id, pid, start_fingerprint, boot_id, scope_nonce, claimed_at):
        return bool(conn.execute("""UPDATE generations SET pid=?,start_fingerprint=?,claim_pending=0,
            boot_id=?,claim_boot_id=?,scope_nonce=?,claim_at=?,heartbeat_at=?
            WHERE id=? AND pid IS NULL AND start_fingerprint IS NULL AND claim_pending=1
              AND state='standby' AND verdict IS NULL""",
            (pid, start_fingerprint, boot_id, boot_id, scope_nonce, claimed_at, time.time(), generation_id)).rowcount)

    def claim_generation(self, generation_id: str, pid: int, start_fingerprint: str,
                         *, boot_id: str | None = None, scope_nonce: str | None = None,
                         claimed_at: float | None = None) -> bool:
        """Claim exactly once; losers exit before touching lease or transport."""
        if int(pid) <= 0 or not start_fingerprint:
            raise ValueError("a claimed generation needs a PID and start fingerprint")
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            changed = self._claim_in_transaction(conn, generation_id, int(pid), str(start_fingerprint),
                boot_id or _boot_id(), scope_nonce, time.time() if claimed_at is None else float(claimed_at))
            conn.commit()
            return changed

    def retire_unclaimed(self, generation_id: str, *, evidence: str = "unclaimed",
                         verdict_at: float | None = None) -> bool:
        return self._retire(generation_id, evidence=evidence, expected_unclaimed=True,
                            verdict_at=verdict_at)

    def _retire(self, generation_id: str, *, evidence: str,
                expected_pid: int | None = None, expected_start_fingerprint: str | None = None,
                expected_unclaimed: bool = False, verdict_at: float | None = None) -> bool:
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            changed = self._retire_in_transaction(
                conn, generation_id, evidence=evidence, expected_pid=expected_pid,
                expected_start_fingerprint=expected_start_fingerprint,
                expected_unclaimed=expected_unclaimed, verdict_at=verdict_at)
            conn.commit()
            return changed

    def _retire_in_transaction(self, conn, generation_id: str, *, evidence: str,
                               expected_pid: int | None = None,
                               expected_start_fingerprint: str | None = None,
                               expected_unclaimed: bool = False,
                               verdict_at: float | None = None) -> bool:
        """Retire under the caller's write fence, together with its cut-work claims."""
        where = ["id=?", "verdict IS NULL"]
        args: list[Any] = [generation_id]
        if expected_unclaimed:
            where.extend(["pid IS NULL", "start_fingerprint IS NULL", "claim_pending=1"])
            where.append("NOT EXISTS(SELECT 1 FROM leases WHERE generation_id=generations.id AND state='active')")
        elif expected_pid is not None:
            where.extend(["pid=?", "start_fingerprint=?"])
            args.extend([expected_pid, expected_start_fingerprint])
        changed = conn.execute(
            "UPDATE generations SET verdict='failed',verdict_at=?,verdict_evidence=?,state='exited',claim_pending=0 "
            f"WHERE {' AND '.join(where)}",
            (time.time() if verdict_at is None else float(verdict_at), evidence, *args),
        ).rowcount
        return bool(changed)

    def retire_dead_generation(self, generation_id: str, *, expected_pid: int,
                               expected_start_fingerprint: str, evidence: str,
                               verdict_at: float | None = None) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM generations WHERE id=?", (generation_id,)).fetchone()
        if (row is None or _is_unclaimed(row) or row["pid"] != expected_pid
                or row["start_fingerprint"] != expected_start_fingerprint):
            return False
        if not self._owner_is_dead(row):
            raise RuntimeError("retirement death proof failed")
        return self._retire(generation_id, evidence=evidence, expected_pid=int(expected_pid),
                            expected_start_fingerprint=expected_start_fingerprint,
                            verdict_at=verdict_at)

    def transition_state(self, generation_id: str, expected_state: str,
                         new_state: str, *, drain_deadline: float | None = None) -> bool:
        if expected_state not in RUNTIME_STATES or new_state not in RUNTIME_STATES:
            raise ValueError("unknown generation runtime state")
        if new_state not in _STATE_TRANSITIONS[expected_state]:
            raise RuntimeError(f"illegal generation transition {expected_state}->{new_state}")
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            changed = conn.execute(
                "UPDATE generations SET state=?,drain_deadline=? WHERE id=? AND state=? AND verdict IS NULL",
                (new_state, drain_deadline, generation_id, expected_state),
            ).rowcount
            conn.commit()
            if not changed:
                raise RuntimeError("generation state compare-and-swap failed")
            return True

    def observe_suspect(self, generation_id: str, *, evidence: str = "heartbeat_expired",
                        observed_at: float | None = None) -> bool:
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            changed = conn.execute(
                "UPDATE generations SET suspect_at=COALESCE(suspect_at,?),suspect_evidence=? "
                "WHERE id=? AND state!='exited'",
                (time.time() if observed_at is None else float(observed_at), evidence, generation_id),
            ).rowcount
            conn.commit()
            return bool(changed)

    def _record_failure(self, generation_id: str, evidence: str) -> bool:
        return self._retire(generation_id, evidence=evidence)

    def heartbeat(self, generation_id: str, *, state: str | None = None) -> None:
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            now = time.time()
            if state is None:
                conn.execute("UPDATE generations SET heartbeat_at=?,suspect_at=NULL,suspect_evidence=NULL WHERE id=?",
                             (now, generation_id))
            else:
                if state not in RUNTIME_STATES:
                    raise ValueError(f"invalid generation runtime state: {state}")
                current = conn.execute("SELECT state,verdict FROM generations WHERE id=?",
                                       (generation_id,)).fetchone()
                if current is None:
                    raise RuntimeError("unknown generation")
                if current["verdict"] is not None and state != current["state"]:
                    raise RuntimeError("generation verdict is terminal")
                if state != current["state"] and state not in _STATE_TRANSITIONS[current["state"]]:
                    raise RuntimeError(f"illegal generation transition {current['state']}->{state}")
                conn.execute("UPDATE generations SET heartbeat_at=?,state=?,suspect_at=NULL,suspect_evidence=NULL WHERE id=? "
                             "AND state=?", (now, state, generation_id, current["state"]))
            conn.commit()

    @with_deadline_scope
    def acquire_lease(self, resource: str, generation_id: str, *, state: str = "active",
                      deadline: float | None = None) -> int:
        with closing(self._deadline_connect(deadline)) as conn, conn:
            self._begin_immediate(conn, deadline)
            self._check_transaction_deadline(conn, deadline)
            candidate = conn.execute("SELECT state,verdict,claim_pending FROM generations WHERE id=?",
                                     (generation_id,)).fetchone()
            if candidate is None:
                raise RuntimeError("unknown generation")
            if candidate["claim_pending"] or candidate["verdict"] is not None or candidate["state"] == "exited":
                raise RuntimeError("generation is not eligible to acquire a lease")
            row = conn.execute("SELECT epoch,generation_id,state FROM leases WHERE resource=?", (resource,)).fetchone()
            if row and row["generation_id"] != generation_id:
                holder = conn.execute("SELECT heartbeat_at,state FROM generations WHERE id=?",
                                      (row["generation_id"],)).fetchone()
                if holder and time.time() - holder["heartbeat_at"] > 5:
                    conn.execute("UPDATE generations SET suspect_at=COALESCE(suspect_at,?) WHERE id=?",
                                 (time.time(), row["generation_id"]))
                    conn.commit()
                    raise RuntimeError("lease is suspect: holder may be alive; explicit takeover required")
                raise RuntimeError("lease is held by another generation; explicit takeover required")
            if candidate["state"] == "draining":
                raise RuntimeError("draining generation cannot acquire a lease")
            if row:
                if row["state"] == "released":
                    raise RuntimeError("released lease requires explicit takeover")
                return int(row["epoch"])
            epoch = 1
            conn.execute("INSERT INTO leases(resource,epoch,generation_id,state) VALUES(?,?,?,?)",
                         (resource, epoch, generation_id, state))
            self._check_transaction_deadline(conn, deadline)
            conn.commit()
            return epoch

    @with_deadline_scope
    def takeover_dead_generation(self, resource: str, old_id: str, new_id: str, *,
                                 bootout: Callable[[str], Any],
                                 death_proof: Callable[[dict[str, Any]], bool] | None = None,
                                 evidence: str = "pid_start_absent",
                                 deadline: float | None = None) -> int:
        """Retire a proven-dead holder, boot it out, then move the lease once."""
        with closing(self._deadline_connect(deadline)) as conn:
            lease = conn.execute("SELECT resource,epoch,generation_id,state FROM leases WHERE resource=?",
                                 (resource,)).fetchone()
            holder = conn.execute("SELECT * FROM generations WHERE id=?", (old_id,)).fetchone()
            successor = conn.execute("SELECT * FROM generations WHERE id=?", (new_id,)).fetchone()
            self._check_transaction_deadline(conn, deadline)
        if not lease or lease["generation_id"] != old_id or lease["state"] not in {"active", "released"}:
            raise RuntimeError("takeover lease changed")
        if holder is None or successor is None or lease["generation_id"] != old_id:
            raise RuntimeError("takeover generation is missing")
        if successor["state"] != "standby" or successor["verdict"] is not None or successor["claim_pending"]:
            raise RuntimeError("takeover successor is not a claimed standby")
        if _is_unclaimed(holder):
            raise RuntimeError("cannot take over an unclaimed holder")
        holder_record = dict(holder)
        clean_exit = holder['state'] == 'exited' and holder['verdict'] is None and lease['state'] == 'released'
        if clean_exit and death_proof is None:
            dead = True
        elif death_proof is None:
            from gateway.status import _get_process_start_time, _pid_exists
            pid = int(holder["pid"])
            alive = _pid_exists(pid)
            actual_start = _get_process_start_time(pid) if alive else None
            dead = (holder["boot_id"] != _boot_id() or not alive or
                    generation_start_fingerprint_matches(holder, actual_start) is False)
            evidence = json.dumps({"reason": evidence, "pid": pid,
                                   "start_fingerprint": holder["start_fingerprint"],
                                   "recorded_boot": holder["boot_id"], "observed_boot": _boot_id(),
                                   "pid_alive": alive, "observed_start": actual_start}, sort_keys=True)
        else:
            dead = bool(death_proof(holder_record))
        if not dead:
            self.observe_suspect(old_id, evidence="death_proof_failed")
            raise RuntimeError("takeover death proof failed")
        with closing(self._deadline_connect(deadline)) as conn, conn:
            self._begin_immediate(conn, deadline)
            self._check_transaction_deadline(conn, deadline)
            current = conn.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
            recorded = conn.execute("SELECT * FROM generations WHERE id=?", (old_id,)).fetchone()
            if (not current or dict(current) != dict(lease) or not recorded
                    or any(recorded[key] != holder[key] for key in ("pid", "start_fingerprint", "boot_id", "label"))):
                raise RuntimeError("takeover retirement compare-and-swap failed")
            if recorded["verdict"] is None and not clean_exit:
                conn.execute("UPDATE generations SET verdict='failed',verdict_at=?,verdict_evidence=?,state='exited' "
                             "WHERE id=?", (time.time(), evidence, old_id))
            elif not clean_exit and (recorded["verdict"] != "failed" or recorded["state"] != "exited"):
                raise RuntimeError("holder has incompatible retirement verdict")
            self._check_transaction_deadline(conn, deadline)
            conn.commit()
        result = bootout(holder["label"])
        if result is not True:
            raise RuntimeError("takeover bootout/readback failed")
        with closing(self._deadline_connect(deadline)) as conn, conn:
            self._begin_immediate(conn, deadline)
            self._check_transaction_deadline(conn, deadline)
            current = conn.execute("SELECT epoch,generation_id,state FROM leases WHERE resource=?",
                                   (resource,)).fetchone()
            if not current or (current["generation_id"], current["state"], current["epoch"]) != (old_id, lease["state"], lease["epoch"]):
                raise RuntimeError("takeover lease changed after bootout")
            retired = conn.execute("SELECT * FROM generations WHERE id=?", (old_id,)).fetchone()
            if (not retired or retired["state"] != "exited" or retired["verdict"] != (None if clean_exit else "failed")
                    or any(retired[key] != holder[key] for key in ("pid", "start_fingerprint", "boot_id", "label"))):
                raise RuntimeError("takeover identity changed after bootout")
            self._close_dead_poller_journal(conn, old_id)
            new_epoch = int(current["epoch"]) + 1
            conn.execute("INSERT INTO lease_moves VALUES(?,?,?,?,?,'takeover')",
                         (resource, old_id, new_id, current["epoch"], new_epoch))
            changed = conn.execute(
                "UPDATE leases SET generation_id=?,epoch=?,state='active' WHERE resource=? "
                "AND generation_id=? AND epoch=? AND state=?",
                (new_id, new_epoch, resource, old_id, current["epoch"], lease["state"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError("takeover lease compare-and-swap failed")
            moved = conn.execute("UPDATE generations SET state='serving',heartbeat_at=? "
                                 "WHERE id=? AND state='standby' AND verdict IS NULL AND claim_pending=0",
                                 (time.time(), new_id)).rowcount
            if moved != 1:
                raise RuntimeError("takeover successor state compare-and-swap failed")
            self._check_transaction_deadline(conn, deadline)
            conn.commit()
            return new_epoch

    @staticmethod
    def _close_dead_poller_journal(conn, generation_id: str) -> None:
        """After death proof and bootout, bound crash intervals before a new poller starts.

        These timestamps are conservative stop/release upper bounds, not the
        unknown crash instant. SIGKILL cannot publish its own closing receipts.
        """
        opened = {}
        for row in conn.execute("SELECT epoch,token_hash,event FROM poller_journal "
                                "WHERE generation_id=? ORDER BY id", (generation_id,)):
            key = (row['epoch'], row['token_hash'])
            opened.setdefault(key, set())
            if row['event'] in {'poller_started', 'lock_acquired'}:
                opened[key].add(row['event'])
            else:
                opened[key].discard({'poller_stopped': 'poller_started',
                                     'lock_released': 'lock_acquired'}[row['event']])
        for (epoch, token), events in opened.items():
            for start, stop in (('poller_started', 'poller_stopped'), ('lock_acquired', 'lock_released')):
                if start in events:
                    conn.execute("INSERT INTO poller_journal(generation_id,epoch,token_hash,event,"
                                 "monotonic_at,wall_at,boot_id) VALUES(?,?,?,?,?,?,?)",
                                 (generation_id, epoch, token, stop, gateway_deadline.now(), time.time(), _boot_id()))

    def record_poller_event(self, token_hash: str, generation_id: str, epoch: int,
                            event: str, *, monotonic_at: float | None = None,
                            wall_at: float | None = None) -> int:
        if not token_hash or event not in _POLL_JOURNAL_EVENTS or type(epoch) is not int or epoch < 1:
            raise ValueError("invalid poller journal event")
        with closing(self.connect()) as conn, conn:
            begin_immediate(conn)
            owner = conn.execute("SELECT boot_id FROM generations WHERE id=?", (generation_id,)).fetchone()
            if owner is None:
                raise RuntimeError("unknown polling generation")
            cursor = conn.execute(
                "INSERT INTO poller_journal(generation_id,epoch,token_hash,event,monotonic_at,wall_at,boot_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (generation_id, int(epoch), token_hash, event,
                 gateway_deadline.now() if monotonic_at is None else float(monotonic_at),
                 time.time() if wall_at is None else float(wall_at), owner["boot_id"]),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def poller_journal(self, *, token_hash: str | None = None) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            if token_hash is None:
                rows = conn.execute("SELECT * FROM poller_journal ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM poller_journal WHERE token_hash=? ORDER BY id",
                                    (token_hash,)).fetchall()
            return [dict(row) for row in rows]

    def check_poller_journal(self, *, token_hash: str | None = None) -> dict[str, Any]:
        return check_poller_journal(self.poller_journal(token_hash=token_hash))


    @with_deadline_scope
    def request_transfer(self, old_id: str, new_id: str, epoch: int, tokens: set[str], *,
                         deadline: float | None = None) -> None:
        """Freeze the expected token roster before asking the old process to stop."""
        with closing(self._deadline_connect(deadline)) as conn, conn:
            self._begin_immediate(conn, deadline)
            self._check_transaction_deadline(conn, deadline)
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            successor = conn.execute("SELECT state,verdict,claim_pending,suspect_at FROM generations WHERE id=?", (new_id,)).fetchone()
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (old_id, epoch, "active"):
                raise RuntimeError("active generation lease changed; transfer refused")
            if not successor or successor["state"] != "standby" or successor["verdict"] is not None or successor["claim_pending"] or successor["suspect_at"] is not None or old_id == new_id:
                raise RuntimeError("successor is not ready")
            old = conn.execute("SELECT state,verdict,suspect_at FROM generations WHERE id=?", (old_id,)).fetchone()
            if not old or old["state"] != "serving" or old["verdict"] is not None or old["suspect_at"] is not None:
                raise RuntimeError("old generation is not serving or is suspect")
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
            self._check_transaction_deadline(conn, deadline)
            conn.commit()

    @with_deadline_scope
    def transfer_attempt_nonce(self, old_id: str, epoch: int, *, deadline: float | None = None) -> str:
        with closing(self._deadline_connect(deadline)) as conn:
            self._check_transaction_deadline(conn, deadline)
            row = conn.execute("SELECT attempt_nonce FROM generation_transfers WHERE old_id=? AND epoch=?",
                               (old_id, epoch)).fetchone()
            self._check_transaction_deadline(conn, deadline)
        if not row or not row["attempt_nonce"]:
            raise RuntimeError("transfer attempt is missing")
        return row["attempt_nonce"]

    @with_deadline_scope
    def record_poller_stopped(self, old_id: str, epoch: int, token_hash: str,
                              safe_offset: int, *, attempt_nonce: str | None = None,
                              deadline: float | None = None) -> None:
        """Persist only a receipt for a token in the frozen roster and current lease."""
        if type(safe_offset) is not int or safe_offset < 0:
            raise RuntimeError("invalid polling cursor")
        with closing(self._deadline_connect(deadline)) as conn, conn:
            self._begin_immediate(conn, deadline)
            self._check_transaction_deadline(conn, deadline)
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
            self._check_transaction_deadline(conn, deadline)
            conn.commit()

    def transfer_receipts(self, old_id: str, epoch: int) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn, conn:
            return [dict(row) for row in conn.execute(
                "SELECT token_hash,safe_offset,poller_stopped FROM transfer_tokens WHERE old_id=? AND epoch=? ORDER BY token_hash",
                (old_id, epoch)).fetchall()]

    @with_deadline_scope
    def commit_transfer(self, old_id: str, new_id: str, epoch: int,
                        *, drain_seconds: float = 7200,
                        deadline: float | None = None) -> int:
        """CAS promotion; a live old holder never loses its lease without its receipts."""
        with closing(self._deadline_connect(deadline)) as conn, conn:
            # BEGIN IMMEDIATE may wait for a writer. The deadline check is
            # intentionally after it: a late lock acquisition must roll back
            # instead of moving the lease after the handover window.
            self._begin_immediate(conn, deadline)
            self._check_transaction_deadline(conn, deadline)
            transfer = conn.execute("SELECT new_id,state FROM generation_transfers WHERE old_id=? AND epoch=?",
                                    (old_id, epoch)).fetchone()
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            successor = conn.execute("SELECT state,verdict,claim_pending,suspect_at FROM generations WHERE id=?", (new_id,)).fetchone()
            if not transfer or transfer["new_id"] != new_id or transfer["state"] != "requested" or not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (old_id, epoch, "active"):
                raise RuntimeError("transfer request or lease changed")
            if not successor or successor["state"] != "standby" or successor["verdict"] is not None or successor["claim_pending"] or successor["suspect_at"] is not None:
                raise RuntimeError("successor is not ready")
            if conn.execute("SELECT 1 FROM transfer_tokens WHERE old_id=? AND epoch=? AND poller_stopped=0 LIMIT 1",
                            (old_id, epoch)).fetchone():
                raise RuntimeError("missing poller stop receipt")
            old = conn.execute("SELECT state,verdict,suspect_at FROM generations WHERE id=?", (old_id,)).fetchone()
            if not old or old["state"] != "serving" or old["verdict"] is not None or old["suspect_at"] is not None:
                raise RuntimeError("old generation is not serving or is suspect")
            conn.execute("INSERT INTO lease_moves VALUES(?,?,?,?,?,'handover')",
                         ("active_generation", old_id, new_id, epoch, epoch + 1))
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
            self._check_transaction_deadline(conn, deadline)
            conn.commit()
            return epoch + 1

    @with_deadline_scope
    def abort_transfer(self, old_id: str, new_id: str, epoch: int,
                       *, attempt_nonce: str, deadline: float | None = None) -> bool:
        """CAS abort against the old lease and exact attempt, never a committed successor."""
        with closing(self._deadline_connect(deadline)) as conn, conn:
            self._begin_immediate(conn, deadline)
            self._check_transaction_deadline(conn, deadline)
            lease = conn.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (old_id, epoch, "active"):
                raise RuntimeError("cannot abort a committed transfer")
            holder = conn.execute("SELECT state,verdict FROM generations WHERE id=?", (old_id,)).fetchone()
            if not holder or holder["state"] != "serving" or holder["verdict"] is not None:
                raise RuntimeError("only a still-serving generation can abort")
            changed = conn.execute(
                "UPDATE generation_transfers SET state='aborted' WHERE old_id=? AND new_id=? "
                "AND epoch=? AND state='requested' AND attempt_nonce=?",
                (old_id, new_id, epoch, attempt_nonce)).rowcount
            self._check_transaction_deadline(conn, deadline)
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
            begin_immediate(conn)
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
            begin_immediate(conn)
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
            begin_immediate(conn)
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
            begin_immediate(conn)
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
    db_path = Path(home) / "gateway-coordinator.db"
    if db_path.exists():
        with closing(connect_sqlite(f"file:{db_path}?mode=ro", uri=True)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(generations)")}
            if "verdict" in columns:
                row = conn.execute("SELECT verdict FROM generations WHERE id=?", (identity.id,)).fetchone()
                if row and row[0] is not None:
                    return  # Failed diagnostics survive cleanup.
    for name, path in generation_paths(home, identity).items():
        if name == "socket":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if payload.get("id") != identity.id or payload.get("start_fingerprint") != identity.start_fingerprint:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def forward_only_handover_enabled(config: Any) -> bool:
    from gateway.config import _coerce_bool
    if isinstance(config, dict):
        gateway = config.get("gateway") or config
        forward = _coerce_bool((gateway.get("forward_only_handover") or {}).get("enabled", False), False)
        legacy = _coerce_bool((gateway.get("overlap_handover") or {}).get("enabled", False), False)
    else:
        forward = getattr(config, "forward_only_handover_enabled", False)
        legacy = getattr(config, "overlap_handover_enabled", False)
    if forward and legacy:
        raise ValueError("gateway.overlap_handover.enabled must be false with gateway.forward_only_handover.enabled")
    return bool(forward)


def overlap_handover_enabled(config: Any) -> bool:
    """The legacy route remains available when the forward-only flag is off."""
    forward = forward_only_handover_enabled(config)
    if isinstance(config, dict):
        legacy = (((config.get("gateway") or config).get("overlap_handover") or {}).get("enabled", False))
    else:
        legacy = getattr(config, "overlap_handover_enabled", False)
    return forward or bool(legacy)


__all__ = [
    "GenerationCoordinator", "GenerationIdentity", "SCHEMA_VERSION",
    "generation_paths", "overlap_handover_enabled", "remove_generation_files",
    "write_generation_record",
]
