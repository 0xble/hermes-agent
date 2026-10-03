"""Coordinator-owned durable session claims and admission rows.

This is a storage boundary, not a native adapter dispatch. Callers must authorize
and canonicalize before enqueueing, and the owner must re-authorize before use.
"""
from __future__ import annotations

import json
import time
from contextlib import closing
from typing import Callable

MAX_ENVELOPE = 16 * 1024
MAX_PAYLOAD = 1024 * 1024


from gateway.deadline import begin_immediate


class OwnedAdmissionMixin:
    def _transaction(self):
        return closing(self.connect())

    def claim_session(self, home: str, transport: str, key: str, owner: str,
                      epoch: int, *, outstanding_work: int = 0) -> bool:
        if outstanding_work < 0:
            raise ValueError("negative outstanding work")
        with self._transaction() as db, db:
            begin_immediate(db)
            if db.execute("SELECT 1 FROM generations WHERE id=?", (owner,)).fetchone() is None:
                raise RuntimeError("unknown session owner")
            changed = db.execute(
                "INSERT OR IGNORE INTO sessions(profile_home,transport,session_key,generation_id,epoch,state,outstanding_work) "
                "VALUES(?,?,?,?,?,'owned',?)",
                (home, transport, key, owner, epoch, outstanding_work),
            ).rowcount
            return bool(changed)

    def freeze_session(self, home: str, transport: str, key: str, owner: str, epoch: int) -> bool:
        """Freeze an existing live claim (or create it) before transferring the lease."""
        with self._transaction() as db, db:
            begin_immediate(db)
            lease = db.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            if lease is None or tuple(lease) != (owner, epoch, "active"):
                raise RuntimeError("cannot freeze a session after the lease moves")
            db.execute(
                "INSERT INTO sessions(profile_home,transport,session_key,generation_id,epoch,state,outstanding_work) "
                "VALUES(?,?,?,?,?,'owned',1) ON CONFLICT(profile_home,transport,session_key) "
                "DO UPDATE SET outstanding_work=1 WHERE generation_id=excluded.generation_id AND epoch=excluded.epoch",
                (home, transport, key, owner, epoch),
            )
            changed = bool(db.execute("SELECT changes()").fetchone()[0])
            if not changed:
                raise RuntimeError("live session is claimed by another generation")
            return True

    def set_outstanding(self, home: str, transport: str, key: str, owner: str,
                        epoch: int, count: int) -> bool:
        if count < 0:
            raise ValueError("negative outstanding work")
        with self._transaction() as db, db:
            begin_immediate(db)
            return bool(db.execute(
                "UPDATE sessions SET outstanding_work=? WHERE profile_home=? AND transport=? "
                "AND session_key=? AND generation_id=? AND epoch=?",
                (count, home, transport, key, owner, epoch),
            ).rowcount)

    def enqueue(self, home: str, transport: str, key: str, event_id: str, kind: str,
                source: bytes, payload: bytes | Callable[[], bytes], active_owner: str, active_epoch: int):
        return self._enqueue(home, transport, key, event_id, kind, source, payload,
                             active_owner, active_epoch, frozen_owner=False)

    def enqueue_owned(self, home: str, transport: str, key: str, event_id: str, kind: str,
                      source: bytes, payload: bytes | Callable[[], bytes], owner: str, epoch: int):
        """Admit a late dispatch to its frozen owner, or forward it to the active owner."""
        return self._enqueue(home, transport, key, event_id, kind, source, payload,
                             owner, epoch, frozen_owner=True)

    def _enqueue(self, home: str, transport: str, key: str, event_id: str, kind: str,
                 source: bytes, payload: bytes | Callable[[], bytes], active_owner: str, active_epoch: int,
                 *, frozen_owner: bool):
        """Commit source and payload before returning a durable-enqueue receipt.

        An existing platform event always wins, even if its later redelivery has a
        changed body. It is never re-admitted by this operation.
        """
        if not all(isinstance(x, str) and x for x in (home, transport, key, event_id, kind)):
            raise ValueError("missing admission identity")
        if not isinstance(source, bytes) or len(source) > MAX_ENVELOPE:
            raise ValueError("invalid source envelope size")
        if not isinstance(payload, bytes) and not callable(payload):
            raise ValueError("invalid event payload")
        if isinstance(payload, bytes) and len(payload) > MAX_PAYLOAD:
            raise ValueError("invalid event payload size")
        try:
            envelope = json.loads(source)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid source envelope") from exc
        if (not isinstance(envelope, dict) or envelope.get("version") != 1
                or envelope.get("authorized") is not True or "sender" not in envelope):
            raise ValueError("source is not an authorized version 1 envelope")
        # Probe only the owner observed under the write fence. A pre-lock probe
        # cannot authorize release and would duplicate the PID/start-time lookup.
        with self._transaction() as db, db:
            begin_immediate(db)
            duplicate = db.execute(
                "SELECT * FROM inbox WHERE profile_home=? AND transport=? AND source_event_id=? AND kind=?",
                (home, transport, event_id, kind),
            ).fetchone()
            if duplicate is not None:
                return dict(duplicate), False
            lease = db.execute(
                "SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'"
            ).fetchone()
            active_lease = (lease is not None and tuple(lease) == (active_owner, active_epoch, "active"))
            if frozen_owner and not active_lease:
                claim = db.execute(
                    "SELECT generation_id,epoch,state,outstanding_work FROM sessions WHERE profile_home=? AND transport=? AND session_key=?",
                    (home, transport, key),
                ).fetchone()
                if (claim is None or tuple(claim)[:3] != (active_owner, active_epoch, "owned")
                        or claim["outstanding_work"] <= 0):
                    if lease is None or lease["state"] != "active":
                        raise RuntimeError("no active generation for late dispatch")
                    # A cannot claim new work while draining. Forward the complete
                    # payload under B's lease, atomically with the claim lookup.
                    active_owner, active_epoch = lease["generation_id"], lease["epoch"]
                    if callable(payload):
                        payload = payload()
            elif not active_lease:
                raise RuntimeError("admission lease is not active for this generation")
            db.execute(
                "INSERT OR IGNORE INTO sessions(profile_home,transport,session_key,generation_id,epoch,state) "
                "VALUES(?,?,?,?,?,'owned')", (home, transport, key, active_owner, active_epoch),
            )
            session = db.execute(
                "SELECT * FROM sessions WHERE profile_home=? AND transport=? AND session_key=?",
                (home, transport, key),
            ).fetchone()
            owner, epoch = session["generation_id"], session["epoch"]
            if owner != active_owner:
                generation = db.execute("SELECT pid,boot_id,start_fingerprint,state FROM generations WHERE id=?",
                                        (owner,)).fetchone()
                if generation is not None and (generation["state"] in ("exited", "failed") or
                                               self._owner_is_dead(generation)):
                    if generation["state"] != "exited":
                        self._retire_in_transaction(
                            db, owner, evidence="admission_owner_dead",
                            expected_pid=generation["pid"],
                            expected_start_fingerprint=generation["start_fingerprint"])
                    self._release_abandoned(db, owner)
                    owner, epoch = active_owner, active_epoch
                    session = db.execute("SELECT * FROM sessions WHERE profile_home=? AND transport=? AND session_key=?",
                                         (home, transport, key)).fetchone()
            if session["state"] == "interrupted":
                # A distinct new inbound event explicitly recovers the session.
                # Pending cut rows remain interrupted and cannot replay on B.
                if owner != active_owner:
                    raise RuntimeError("interrupted owner is unavailable for recovery")
                db.execute("UPDATE sessions SET state='owned' WHERE profile_home=? AND transport=? AND session_key=?",
                           (home, transport, key))
            # A session with no work and no queued input may move to the active
            # generation atomically with the first subsequent admission.
            # Older local admissions left a non-replayable placeholder pending.
            # Settle it before testing replay order so it cannot wedge this lane.
            db.execute("UPDATE inbox SET state='accepted' WHERE profile_home=? AND transport=? "
                       "AND session_key=? AND state='pending' AND payload=?",
                       (home, transport, key, b"{}"))
            pending = db.execute(
                "SELECT 1 FROM inbox WHERE profile_home=? AND transport=? AND session_key=? AND state='pending' LIMIT 1",
                (home, transport, key),
            ).fetchone()
            if owner != active_owner and not session["outstanding_work"] and pending is None:
                db.execute(
                    "UPDATE sessions SET generation_id=?,epoch=? WHERE profile_home=? AND transport=? AND session_key=?",
                    (active_owner, active_epoch, home, transport, key),
                )
                owner, epoch = active_owner, active_epoch
            elif owner != active_owner:
                generation = db.execute("SELECT state FROM generations WHERE id=?", (owner,)).fetchone()
                if generation is None or generation["state"] not in ("draining", "serving"):
                    raise RuntimeError("session owner is unavailable; event remains unacknowledged")
            local_placeholder = callable(payload) and owner == active_owner and pending is None
            if callable(payload):
                payload = b"{}" if local_placeholder else payload()
            if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD:
                raise ValueError("invalid event payload size")
            seq = session["last_seq"] + 1
            db.execute("UPDATE sessions SET last_seq=? WHERE profile_home=? AND transport=? AND session_key=?",
                       (seq, home, transport, key))
            cursor = db.execute(
                "INSERT INTO inbox(profile_home,transport,session_key,source_event_id,kind,seq,owner_id,"
                "owner_epoch,authorized_source,payload,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (home, transport, key, event_id, kind, seq, owner, epoch, source, payload,
                 "accepted" if local_placeholder else "pending", time.time()),
            )
            return dict(db.execute("SELECT * FROM inbox WHERE id=?", (cursor.lastrowid,)).fetchone()), True

    def prune_settled_inbox(self, owner: str, live_keys: set[str]) -> int:
        """Retain seven days of settled evidence, never a transport replay obligation.

        Seven days matches generation/transfer history and exceeds the wire
        journal's 24-hour settled-evidence retention. The native poller's claim gate remains
        authoritative after cleanup. Live buffers and received/processing wire
        rows retain their receipts. Unattributed legacy rows stay put rather
        than guessing which token owns them. A 1,000-row batch each minute
        bounds write-lock work without putting database I/O on the event loop.
        """
        with self._transaction() as db, db:
            begin_immediate(db)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('telegram_updates','polling_cursors')")}
            if tables != {"telegram_updates", "polling_cursors"}:
                return 0
            return db.execute(
                "DELETE FROM inbox WHERE id IN (SELECT i.id FROM inbox i "
                "JOIN sessions s USING(profile_home,transport,session_key) "
                "JOIN generations g ON g.id=i.owner_id "
                "WHERE i.state IN ('accepted','refused','interrupted') AND i.created_at>0 AND i.created_at<? "
                "AND (i.owner_id=? OR g.state IN ('exited','failed')) AND s.outstanding_work=0 "
                "AND i.session_key NOT IN (SELECT value FROM json_each(?)) "
                "AND i.transport='telegram' AND i.source_event_id!='' AND i.source_event_id NOT GLOB '*[^0-9]*' "
                "AND EXISTS (SELECT 1 FROM polling_cursors c WHERE c.token_hash="
                "json_extract(CAST(i.authorized_source AS TEXT),'$.token_hash')) "
                "AND NOT EXISTS (SELECT 1 FROM telegram_updates u WHERE u.token_hash="
                "json_extract(CAST(i.authorized_source AS TEXT),'$.token_hash') "
                "AND u.update_id=CAST(i.source_event_id AS INTEGER) AND u.state IN ('received','processing')) "
                "ORDER BY i.created_at,i.id LIMIT 1000)",
                (time.time() - 7 * 86400, owner, json.dumps(sorted(live_keys))),
            ).rowcount

    def pending(self, owner: str, home: str, transport: str, key: str) -> list[dict]:
        with self._transaction() as db:
            return [dict(row) for row in db.execute(
                "SELECT i.* FROM inbox i JOIN sessions s USING(profile_home,transport,session_key) "
                "WHERE i.owner_id=? AND i.profile_home=? AND i.transport=? AND i.session_key=? "
                "AND i.owner_id=s.generation_id AND i.owner_epoch=s.epoch AND i.state='pending' ORDER BY i.seq",
                (owner, home, transport, key),
            )]

    def disposition(self, row_id: int, owner: str, epoch: int, state: str) -> bool:
        if state not in ("accepted", "refused"):
            raise ValueError("terminal disposition must be accepted or refused")
        with self._transaction() as db, db:
            begin_immediate(db)
            row = db.execute("SELECT * FROM inbox WHERE id=?", (row_id,)).fetchone()
            if row is None or row["state"] != "pending" or row["owner_id"] != owner or row["owner_epoch"] != epoch:
                return False
            session = db.execute(
                "SELECT generation_id,epoch FROM sessions WHERE profile_home=? AND transport=? AND session_key=?",
                (row["profile_home"], row["transport"], row["session_key"]),
            ).fetchone()
            if session is None or session["generation_id"] != owner or session["epoch"] != epoch:
                return False
            earlier = db.execute(
                "SELECT 1 FROM inbox WHERE profile_home=? AND transport=? AND session_key=? AND seq<? "
                "AND state='pending' LIMIT 1",
                (row["profile_home"], row["transport"], row["session_key"], row["seq"]),
            ).fetchone()
            if earlier:
                return False
            return bool(db.execute("UPDATE inbox SET state=? WHERE id=? AND state='pending'",
                                   (state, row_id)).rowcount)

    def interrupt_row(self, row_id: int, owner: str, epoch: int) -> bool:
        """Fence a cut or failed dispatch; duplicate updates must never replay it."""
        with self._transaction() as db, db:
            begin_immediate(db)
            return bool(db.execute("UPDATE inbox SET state='interrupted' WHERE id=? AND owner_id=? "
                                   "AND owner_epoch=? AND state='pending'", (row_id, owner, epoch)).rowcount)

    @staticmethod
    def _owner_is_dead(record) -> bool:
        from gateway.status import _get_process_start_time, _pid_exists
        from gateway.generation import _boot_id, generation_start_fingerprint_matches
        pid = int(record["pid"])
        alive = _pid_exists(pid)
        start = _get_process_start_time(pid) if alive else None
        return (record["boot_id"] != _boot_id() or not alive or
                generation_start_fingerprint_matches(record, start) is False)

    def hold_dead_owner(self, owner: str) -> int:
        """Interrupt pending rows only with PID/start-fingerprint death proof."""
        with self._transaction() as db, db:
            begin_immediate(db)
            record = db.execute("SELECT pid,boot_id,start_fingerprint FROM generations WHERE id=?",
                                (owner,)).fetchone()
            if record is None:
                raise RuntimeError("unknown owner; cannot prove death")
            if not self._owner_is_dead(record):
                return 0
            self._retire_in_transaction(
                db, owner, evidence="admission_owner_dead", expected_pid=record["pid"],
                expected_start_fingerprint=record["start_fingerprint"])
            return self._release_abandoned(db, owner)

    @staticmethod
    def _release_abandoned(db, owner: str) -> int:
        """Fence cut work and transfer claims atomically to the live lease holder."""
        # A cut claim is not an ordinary drained release. Preserve that fact
        # across transfer; only a distinct new inbound event can recover it.
        db.execute("UPDATE sessions SET state='interrupted' WHERE generation_id=? "
                   "AND (outstanding_work>0 OR EXISTS (SELECT 1 FROM inbox i WHERE "
                   "i.profile_home=sessions.profile_home AND i.transport=sessions.transport "
                   "AND i.session_key=sessions.session_key AND i.owner_id=? AND i.state='pending'))",
                   (owner, owner))
        interrupted = db.execute("UPDATE inbox SET state='interrupted' WHERE owner_id=? AND state='pending'",
                                 (owner,)).rowcount
        lease = db.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
        if lease and lease["state"] == "active" and lease["generation_id"] != owner:
            db.execute("UPDATE sessions SET generation_id=?,epoch=?,outstanding_work=0 "
                       "WHERE generation_id=?", (lease["generation_id"], lease["epoch"], owner))
        else:
            db.execute("UPDATE sessions SET outstanding_work=0 WHERE generation_id=?", (owner,))
        return interrupted

    def release_exited_owner(self, owner: str) -> int:
        with self._transaction() as db, db:
            begin_immediate(db)
            record = db.execute("SELECT state FROM generations WHERE id=?", (owner,)).fetchone()
            if record is None or record["state"] != "exited":
                raise RuntimeError("owner has not exited")
            return self._release_abandoned(db, owner)

    def transfer_session(self, home: str, transport: str, key: str, old: str,
                         old_epoch: int, new: str, new_epoch: int) -> bool:
        with self._transaction() as db, db:
            begin_immediate(db)
            lease = db.execute("SELECT generation_id,epoch,state FROM leases WHERE resource='active_generation'").fetchone()
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (new, new_epoch, "active"):
                return False
            changed = db.execute(
                "UPDATE sessions SET generation_id=?,epoch=? WHERE profile_home=? AND transport=? "
                "AND session_key=? AND generation_id=? AND epoch=? AND outstanding_work=0",
                (new, new_epoch, home, transport, key, old, old_epoch),
            ).rowcount
            if changed:
                db.execute(
                    "UPDATE inbox SET owner_id=?,owner_epoch=? WHERE profile_home=? AND transport=? "
                    "AND session_key=? AND owner_id=? AND owner_epoch=? AND state='pending'",
                    (new, new_epoch, home, transport, key, old, old_epoch),
                )
            return bool(changed)
