"""Durable source operation references; never store or replay source content."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

from hermes_constants import get_hermes_home
from .source_retention import SourceCandidate


class SourceJournal:
    def __init__(self, endpoint: str, bank_id: str):
        self.scope = hashlib.sha256(json.dumps([endpoint, bank_id]).encode()).hexdigest()
        self.path = get_hermes_home() / "memories" / "hindsight-source-operations.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS source_operations (
                scope TEXT NOT NULL, source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                sequence INTEGER NOT NULL, entry TEXT NOT NULL,
                PRIMARY KEY(scope, source_id, content_hash))""")
            db.execute("""CREATE TABLE IF NOT EXISTS source_submissions (
                scope TEXT NOT NULL, source_id TEXT NOT NULL, token TEXT NOT NULL,
                content_hash TEXT NOT NULL, PRIMARY KEY(scope, source_id))""")
        self.path.chmod(0o600)

    def reserve(self, candidate):
        """Same-journal admission. An unreceipted request never expires automatically."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM source_submissions WHERE scope=? AND source_id=?",
                          (self.scope, candidate.source_id)).fetchone():
                return None
            rows = db.execute("SELECT entry FROM source_operations WHERE scope=? AND source_id=?",
                              (self.scope, candidate.source_id)).fetchall()
            for (raw,) in rows:
                entry = json.loads(raw)
                pending = set(entry.get("pending_operation_ids", []))
                terminal = set(entry.get("terminal_operation_ids", []))
                if pending - terminal or (entry["status"] == "accepted" and not pending):
                    return None
            token = uuid.uuid4().hex
            db.execute("INSERT INTO source_submissions VALUES (?, ?, ?, ?)",
                       (self.scope, candidate.source_id, token, candidate.content_hash))
            return token

    def release(self, candidate, token):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM source_submissions WHERE scope=? AND source_id=? AND token=?",
                       (self.scope, candidate.source_id, token))

    def unresolved_submission(self, source_id=None):
        with closing(sqlite3.connect(self.path)) as db:
            if source_id is None:
                return db.execute("SELECT 1 FROM source_submissions WHERE scope=? LIMIT 1",
                                  (self.scope,)).fetchone() is not None
            return db.execute("SELECT 1 FROM source_submissions WHERE scope=? AND source_id=?",
                              (self.scope, source_id)).fetchone() is not None

    def load(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT entry FROM source_operations WHERE scope=? ORDER BY sequence",
                              (self.scope,)).fetchall()
        entries = {}
        for (raw,) in rows:
            entry = json.loads(raw)
            entry["candidate"] = SourceCandidate(**entry["candidate"])
            if "superseded_by" in entry:
                entry["superseded_by"] = tuple(entry["superseded_by"])
            entries[entry["candidate"].automatic_key] = entry
        return entries

    def save(self, entry, *, accepted=False, submission_token=None):
        candidate = entry["candidate"]
        entry = dict(entry)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT entry FROM source_operations WHERE scope=? AND source_id=? AND content_hash=?",
                             (self.scope, candidate.source_id, candidate.content_hash)).fetchone()
            if row:
                previous = json.loads(row[0])
                known = set(entry.get("operation_ids", []))
                pending = set(entry.get("pending_operation_ids", []))
                previous_known = set(previous.get("operation_ids", []))
                previous_pending = set(previous.get("pending_operation_ids", []))
                if not accepted and entry.get("sequence", 0) < previous.get("sequence", 0):
                    # Old completion/supersession proves nothing about a newer
                    # acceptance of the same hash. Merge only operation evidence.
                    entry = dict(previous, candidate=candidate)
                # Concurrent provider instances can accept the same hash. An
                # update may retire only IDs it actually observed as terminal.
                entry["operation_ids"] = sorted(known | previous_known)
                entry["terminal_operation_ids"] = sorted(
                    set(entry.get("terminal_operation_ids", [])) | set(previous.get("terminal_operation_ids", [])))
                entry["pending_operation_ids"] = sorted(
                    (pending | previous_pending) - (known - pending) - (previous_known - previous_pending))
                entry["sequence"] = max(entry.get("sequence", 0), previous.get("sequence", 0))
                if (not accepted and entry["status"] in {"queued", "accepted"}
                        and previous["status"] in {"completed", "superseded", "failed"}):
                    entry["status"] = previous["status"]
                    if "superseded_by" in previous:
                        entry["superseded_by"] = tuple(previous["superseded_by"])
                if previous["status"] == "failed" and not accepted:
                    entry["status"] = "failed"
                    entry.pop("superseded_by", None)
            if accepted:
                entry["sequence"] = db.execute(
                    "SELECT COALESCE(MAX(sequence), 0)+1 FROM source_operations WHERE scope=?",
                    (self.scope,)).fetchone()[0]
            payload = dict(entry, candidate={
                "source_type": candidate.source_type, "source_id": candidate.source_id,
                "content_hash": candidate.content_hash, "context": "",
            })
            db.execute("""INSERT INTO source_operations VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, source_id, content_hash) DO UPDATE SET
                sequence=excluded.sequence, entry=excluded.entry""",
                (self.scope, candidate.source_id, candidate.content_hash,
                 entry.get("sequence", 0), json.dumps(payload)))
            if accepted and submission_token and entry.get("pending_operation_ids"):
                # Receipt persistence and handoff are one transaction: a crash
                # cannot leave a known operation hidden behind an unknown claim.
                db.execute("DELETE FROM source_submissions WHERE scope=? AND source_id=? AND token=?",
                           (self.scope, candidate.source_id, submission_token))
        return entry


def restore_source_ledger(provider, bank_id, *, recover_only=False, refresh=False):
    """Lazy recovery after endpoint/bank configuration, without resubmitting writes."""
    scope = (getattr(provider, "_api_url", ""), bank_id)
    with provider._source_retain_keys_lock:
        existing = getattr(provider, "_source_journal_scope", None)
        if existing == scope and not refresh:
            return
        if existing is not None and existing != scope:
            raise ValueError("Cannot change Hindsight source ledger scope within a provider lifecycle")
        if recover_only and not (get_hermes_home() / "memories" / "hindsight-source-operations.sqlite").exists():
            return
        journal = provider._source_journal if existing == scope else SourceJournal(*scope)
        entries = journal.load()
        for key, entry in entries.items():
            # Operation references refresh across providers, payloads stay local.
            local = provider._source_ledger.get(key)
            if local is not None:
                known = set(entry.get("operation_ids", []))
                pending = set(entry.get("pending_operation_ids", []))
                local_known = set(local.get("operation_ids", []))
                local_pending = set(local.get("pending_operation_ids", []))
                terminal = set(entry.get("terminal_operation_ids", [])) | set(local.get("terminal_operation_ids", []))
                if local.get("sequence", 0) > entry.get("sequence", 0):
                    # A failed journal save cannot erase a newer acceptance that
                    # this process still owns. Its unresolved claim blocks peers.
                    entry = dict(local)
                else:
                    entry["candidate"] = local["candidate"]
                entry["operation_ids"] = sorted(known | local_known)
                entry["pending_operation_ids"] = sorted(
                    (pending | local_pending) - (known - pending) - (local_known - local_pending))
                entry["terminal_operation_ids"] = sorted(terminal)
            provider._source_ledger[key] = entry
        previous_source_ops = set(provider._source_retain_ops)
        provider._source_retain_ops.clear()
        for key, entry in provider._source_ledger.items():
            provider._source_terminal_ops.update(entry.get("terminal_operation_ids", []))
            if entry["status"] == "completed":
                provider._source_retain_verified.add(key)
            else:
                provider._source_retain_verified.discard(key)
            for op_id in entry.get("pending_operation_ids", []):
                provider._source_retain_ops[op_id] = entry["candidate"]
        with provider._pending_retain_ops_lock:
            provider._pending_retain_ops.difference_update(previous_source_ops - provider._source_retain_ops.keys())
            provider._pending_retain_ops.update(provider._source_retain_ops)
            if provider._source_retain_ops:
                provider._retain_ops_bank_id = bank_id
        provider._source_terminal_ops.intersection_update(provider._source_retain_ops)
        provider._source_journal = journal
        provider._source_journal_scope = scope


def save_source_entry(provider, candidate, *, accepted=False, submission_token=None, **changes):
    """Caller holds the source lock. Preserve acceptance order and operation evidence."""
    entry = dict(provider._source_ledger.get(candidate.automatic_key, {}),
                 candidate=candidate, **changes)
    if entry.get("status") != "superseded":
        entry.pop("superseded_by", None)
    if accepted:
        previous = provider._source_ledger.get(candidate.automatic_key, {})
        entry["operation_ids"] = sorted(set(previous.get("operation_ids", []))
                                        | set(entry.get("operation_ids", [])))
        entry["sequence"] = max((e.get("sequence", 0) for e in provider._source_ledger.values()), default=0) + 1
    # Memory remains evidence if the local journal write fails. Never turn an
    # accepted remote operation into a retryable failed submission on disk error.
    provider._source_ledger[candidate.automatic_key] = entry
    journal = getattr(provider, "_source_journal", None)
    if journal is not None:
        entry = journal.save(entry, accepted=accepted, submission_token=submission_token)

    provider._source_ledger[candidate.automatic_key] = entry


def supersede_source(provider, candidate, stored_hash):
    """Current readback plus later verified acceptance, never completion order alone."""
    with provider._source_retain_keys_lock:
        entry = provider._source_ledger.get(candidate.automatic_key, {})
        sequence = entry.get("sequence")
        if sequence is None:
            return False
        journal = getattr(provider, "_source_journal", None)
        entries = journal.load() if journal is not None else provider._source_ledger
        sequence = max(sequence, entries.get(candidate.automatic_key, {}).get("sequence", 0))
        for key, replacement in entries.items():
            if (key == (candidate.source_id, stored_hash)
                    and replacement.get("sequence", 0) > sequence
                    and replacement["status"] == "completed"
                    and not replacement.get("pending_operation_ids")):
                save_source_entry(provider, candidate, status="superseded", superseded_by=key)
                return True
    return False


def finish_source_operation(provider, op_id):
    with provider._source_retain_keys_lock:
        candidate = provider._source_retain_ops.get(op_id)
        if candidate is not None:
            entry = provider._source_ledger.get(candidate.automatic_key, {})
            remaining = [op for op in entry.get("pending_operation_ids", []) if op != op_id]
            save_source_entry(provider, candidate, pending_operation_ids=remaining)
            # A concurrent instance may have journaled another operation for
            # this hash since our snapshot. Adopt its pending reference, not its write.
            remaining = provider._source_ledger[candidate.automatic_key].get("pending_operation_ids", [])
            for pending_id in remaining:
                provider._source_retain_ops[pending_id] = candidate
            with provider._pending_retain_ops_lock:
                provider._pending_retain_ops.update(remaining)
            provider._source_retain_ops.pop(op_id, None)
