"""JSONL session graph and exact-segment audit streams, separate from model-tool size caps."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys

from hermes_state_evidence import (
    GRAPH_COLUMNS, MAX_SOURCE_ROW_BYTES, EvidenceUnavailable, evidence_snapshot, inventory_rows, validate_projection,
)

# JSON escaping can expand a source byte sixfold. Keep a separate hard output ceiling too.
MAX_RECORD_BYTES = 2 * 1024 * 1024
_IDENTITY_FIELDS = frozenset({"id", "session_id", "parent_session_id", "message_uid", "absorbed_message_uids",
                                  "user_id", "chat_id", "session_key"})


def _redact_row(row: dict) -> dict:
    from agent.redact import redact_sensitive_text

    result = {}
    for key, value in row.items():
        cleaned = redact_sensitive_text(value, force=True) if isinstance(value, str) else value
        if key in _IDENTITY_FIELDS and cleaned != value:
            session = redact_sensitive_text(str(row.get("session_id", row.get("id", "unknown"))), force=True)
            raise EvidenceUnavailable(
                f"source identity requires redaction for field {key} in session {session!r}; exact stream unavailable")
        result[key] = cleaned
    return result


def _encoded(record: dict) -> bytes:
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise EvidenceUnavailable(f"record exceeds {MAX_RECORD_BYTES} encoded bytes")
    return data


class _Stream:
    def __init__(self):
        self.digest = hashlib.sha256()
        self.inventory_digest = None
        self.hashed_bytes = 0
        self.count = 0

    def emit(self, record: dict) -> None:
        data = _encoded(record)
        self.digest.update(data)
        self.hashed_bytes += len(data)
        _write(data)

    def finish(self) -> None:
        _write(_encoded({"type": "footer", "complete": True, "exhausted": True,
                         "count": self.count, "sha256": self.digest.hexdigest(),
                         "hashed_bytes": self.hashed_bytes,
                         **({"inventory_sha256": self.inventory_digest.hexdigest()}
                            if self.inventory_digest is not None else {})}))


def _write(data: bytes) -> None:
    # Writing the bytes directly prevents host newline/encoding settings from changing the hash.
    if hasattr(sys.stdout, "buffer"):
        written, expected = sys.stdout.buffer.write(data), len(data)
    else:
        text = data.decode("utf-8")
        written, expected = sys.stdout.write(text), len(text)
    if written != expected:
        raise EvidenceUnavailable("short output write, no complete evidence")
    sys.stdout.flush()


def _capture(stream: _Stream, *, home, profile: str, session_id: str | None, policy=None) -> None:
    transcript = session_id is not None
    with evidence_snapshot(home / "state.db") as conn:
        from hermes_state_acquisition import acquire_rows, acquisition_preflight
        validate_projection(conn)
        if transcript:
            acquisition_preflight(conn, session_id, policy)
        origin = {"id": session_id} if transcript else None
        header = {
            "type": "header", "version": 1,
            "stream": "session_transcript" if transcript else "session_inventory",
            "scope": _redact_row({"home": str(home), "profile": profile}),
            "snapshot": "sqlite_read_transaction", "order": "messages.id" if transcript else "sessions.id_binary",
            "projection": list(GRAPH_COLUMNS),
            "max_source_row_bytes": MAX_SOURCE_ROW_BYTES, "max_record_bytes": MAX_RECORD_BYTES,
            "redaction": "agent.redact.redact_sensitive_text(force=True)",
            "hash": "sha256 of exact UTF-8 JSONL bytes before footer, including header and LF",
        }
        if transcript:
            header.update(session=origin, human_origin="per_event_preflight", version=2,
                          projection=["id", "session_id", "message_uid", "role", "timestamp", "origin",
                                      "disposition", "content", "authored_spans", "principal", "platform", "event"])
        stream.emit(header)
        rows = acquire_rows(conn, session_id, policy) if transcript else inventory_rows(conn)
        if not transcript:
            stream.inventory_digest = hashlib.sha256()
        try:
            for row in rows:
                evidence = _redact_row(row)
                if not transcript:
                    stream.inventory_digest.update(json.dumps(
                        {key: row[key] for key in ("id", "parent_session_id", "source", "created_source")},
                        sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n")
                record = ({"type": "message", "message": evidence, "human_origin": row["origin"]}
                          if transcript else {"type": "session", "session": evidence})
                stream.emit(record)
                stream.count += 1
        finally:
            rows.close()


def cmd_session_evidence(args) -> int:
    from hermes_constants import get_hermes_home, profile_name_for_home
    try:
        home = get_hermes_home()
        profile = profile_name_for_home(home) or "default"
        session_id = args.session_id if args.state_action == "transcript" else None
        if session_id is not None and (not session_id or len(session_id.encode("utf-8")) > 4096):
            raise EvidenceUnavailable("one nonempty exact session ID of at most 4096 bytes is required")
        stream = _Stream()
        policy = _load_policy(args.control_roots_file) if session_id is not None else None
        _capture(stream, home=home, profile=profile, session_id=session_id, policy=policy)
        stream.finish()
        return 0
    except BrokenPipeError:
        # Discard buffered output so interpreter shutdown cannot flush into the closed pipe.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 1
    except KeyboardInterrupt:
        print('{"type":"error","error":"stream interrupted, no complete evidence"}', file=sys.stderr)
        return 130
    except (EvidenceUnavailable, OSError, sqlite3.Error, UnicodeError, ValueError) as exc:
        # SQLite errors can contain source values. Report the failure class, never raw payloads.
        reason = str(exc) if isinstance(exc, EvidenceUnavailable) else type(exc).__name__
        print(json.dumps({"type": "error", "error": reason}), file=sys.stderr)
        return 1


def _load_policy(path):
    from pathlib import Path
    from hermes_state_acquisition import AcquisitionPolicy
    with Path(path).open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise EvidenceUnavailable("control coverage manifest exceeds 64 KiB")
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("version") != 1 or not data.get("owner")
            or not isinstance(data.get("roots"), list)
            or not all(isinstance(root, str) and root for root in data["roots"])):
        raise EvidenceUnavailable("invalid owner control coverage manifest")
    return AcquisitionPolicy(tuple(data["roots"]), data.get("inventory_sha256"), data.get("coverage_complete") is True)
