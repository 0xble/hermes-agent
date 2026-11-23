"""Retention pruning, stale-session archiving and VACUUM policy mixin for SessionDB."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, cast

import gzip
import json
import logging
import os
import time
import zlib

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl: Any = None

from hermes_state_common import (
    AUTO_VACUUM_MIN_FREELIST_RATIO, _id_chunks, _non_continuation_child_sql, _placeholders, _sql_session_last_active,
    escape_like as _escape_like, emit_session_deleted,
)
from hermes_startup_watchdog import report_startup_progress
from hermes_state_deletion import finish_pending_deletes, stage_delete, visible_session_sql
from hermes_state_prune_pacing import DeletionWindowClosedError, PrunePacer, prune_pass

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")

_LAST_ACTIVE_SQL = _sql_session_last_active("s")
_TOKENS_SQL = "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0))"
_COST_SQL = "COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0)"


def _like(value: str) -> str:
    return f"%{_escape_like(value.lower())}%"


def _cwd_prefix_filter(value: str) -> tuple[list[str], list]:
    from hermes_state_sessions import _cwd_prefix_clause
    clause, params = _cwd_prefix_clause(value)
    return [clause], list(params)


def _one(clause: str, conv=None):
    return lambda v: ([clause], [conv(v) if conv else v])


def _seconds_since(now: float, raw) -> Optional[float]:
    """Age of a state_meta timestamp; None when unset or corrupt (= no prior run)."""
    try:
        return now - float(raw) if raw else None
    except (TypeError, ValueError):
        return None


# Prune/archive filters in evaluation order: (kwarg, applies-when, builder -> (clauses, params)).
# ``applies-when``: "notnone" (numeric/time bounds; 0 is a real bound) or "truthy" ("" = unset).
_PRUNE_FILTERS = (
    # Orphan-swept rows age from the sweep, not old activity, or the next prune deletes them before recovery.
    ("last_active_before", "notnone", lambda v: (
        [_LAST_ACTIVE_SQL + " < ?",
         "(COALESCE(s.end_reason, '') != 'startup_orphan_reap' OR s.ended_at < ?)"],
        [v, v])),
    ("last_active_after", "notnone", _one(_LAST_ACTIVE_SQL + " >= ?")),
    ("started_before", "notnone", _one("s.started_at < ?")),
    ("started_after", "notnone", _one("s.started_at >= ?")),
    ("source", "truthy", _one("s.source = ?")),
    ("title_like", "truthy", _one("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'", _like)),
    ("end_reason", "truthy", _one("s.end_reason = ?")),
    ("cwd_prefix", "truthy", _cwd_prefix_filter),
    ("min_messages", "notnone", _one("s.message_count >= ?")),
    ("max_messages", "notnone", _one("s.message_count <= ?")),
    ("model_like", "truthy", _one("LOWER(COALESCE(s.model, '')) LIKE ? ESCAPE '\\'", _like)),
    ("provider", "truthy", _one("LOWER(COALESCE(s.billing_provider, '')) = ?", str.lower)),
    ("user_id", "truthy", _one("s.user_id = ?")),
    ("chat_id", "truthy", _one("s.chat_id = ?")),
    ("chat_type", "truthy", _one("s.chat_type = ?")),
    ("branch_like", "truthy", _one("LOWER(COALESCE(s.git_branch, '')) LIKE ? ESCAPE '\\'", _like)),
    ("min_tokens", "notnone", _one(_TOKENS_SQL + " >= ?")),
    ("max_tokens", "notnone", _one(_TOKENS_SQL + " <= ?")),
    ("min_cost", "notnone", _one(_COST_SQL + " >= ?")),
    ("max_cost", "notnone", _one(_COST_SQL + " <= ?")),
    ("min_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) >= ?")),
    ("max_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) <= ?")),
)
_PRUNE_FILTER_NAMES = frozenset(name for name, _, _ in _PRUNE_FILTERS) | {"archived", "include_pinned", "lineage_tips_only"}

# Child ``c`` continues compression-ended ``p`` (same predicate as compression's child lookup).
_CONTINUATION_EDGE_SQL = "p.end_reason = 'compression'\n" + _non_continuation_child_sql("c.", "p.id")


def _continued_ancestors_sql(candidates_where: str) -> str:
    """Compression ancestors of every row *candidates_where* (alias ``s``) does not select."""
    return ("WITH RECURSIVE kept(id) AS ("
            " SELECT p.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            f" AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = c.id AND {candidates_where})"
            " UNION"
            " SELECT p.id FROM kept k JOIN sessions c ON c.id = k.id JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            ") SELECT id FROM kept")


# A pin covers the whole conversation, but a store can hold a pinned segment whose later
# continuations were published unpinned; those still belong to the pinned chat.
_PINNED_TAIL_SQL = ("WITH RECURSIVE tail(id) AS ("
                    " SELECT c.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
                    f" WHERE COALESCE(p.pinned, 0) = 1 AND {_CONTINUATION_EDGE_SQL}"
                    " UNION"
                    " SELECT c.id FROM tail t JOIN sessions p ON p.id = t.id JOIN sessions c ON c.parent_session_id = p.id"
                    f" WHERE {_CONTINUATION_EDGE_SQL}"
                    ") SELECT id FROM tail")


def _not_pinned_sql(alias: str = "s") -> str:
    """Predicate sparing pinned rows and the unpinned continuations a pinned segment covers."""
    return f"COALESCE({alias}.pinned, 0) = 0 AND {alias}.id NOT IN ({_PINNED_TAIL_SQL})"


_COMPACTED_ARCHIVE_FORMAT = "hermes-compacted-history-v1"
_COMPACTED_ARCHIVE_READ_CHUNK = 1024 * 1024
_COMPACTED_ARCHIVE_INFLATE_LIMIT = 1024 * 1024


def _sync_fd(fd: int) -> None:
    """Flush an fd, using macOS's drive-cache barrier when available."""
    os.fsync(fd)
    fullsync = getattr(fcntl, "F_FULLFSYNC", None) if fcntl is not None else None
    if fullsync is not None:
        try:
            fcntl.fcntl(fd, fullsync)
        except OSError:
            # Some filesystems expose the constant but reject the operation;
            # the fsync above is the portable fallback.
            pass


def _fsync_directory(path: Path) -> None:
    """Persist a newly created archive directory where the platform supports it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        _sync_fd(fd)
    finally:
        os.close(fd)


def _read_compacted_member(
    raw, chunk: bytes, on_line: Callable[[bytes], None],
) -> tuple[bytes, int, bool]:
    """Stream one gzip member, calling *on_line* for each complete JSONL line."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    line_buffer = bytearray()

    def consume(payload: bytes) -> None:
        line_buffer.extend(payload)
        start = 0
        while True:
            newline = line_buffer.find(b"\n", start)
            if newline < 0:
                break
            if newline > start:
                on_line(bytes(line_buffer[start:newline]))
            start = newline + 1
        if start:
            del line_buffer[:start]

    while chunk:
        # Bound each inflate step: highly compressible rows can expand a 1 MiB
        # read into hundreds of MiB, which would defeat streaming recovery.
        data = chunk
        while True:
            try:
                out = decompressor.decompress(data, _COMPACTED_ARCHIVE_INFLATE_LIMIT)
            except zlib.error as exc:
                raise ValueError("invalid gzip member in compacted-history archive") from exc
            consume(out)
            data = decompressor.unconsumed_tail
            # A full output buffer may leave inflated bytes pending inside zlib
            # even after all input is consumed; drain before declaring the
            # member incomplete, or a complete final member could be truncated.
            if decompressor.eof or (not data and len(out) < _COMPACTED_ARCHIVE_INFLATE_LIMIT):
                break
        if decompressor.eof:
            if line_buffer:
                on_line(bytes(line_buffer))
            pending = decompressor.unused_data
            return pending, raw.tell() - len(pending), True
        chunk = raw.read(_COMPACTED_ARCHIVE_READ_CHUNK)
    return b"", 0, False


def _read_compacted_archive(path: Path) -> tuple[Optional[dict[str, object]], dict[int, tuple], bool, int]:
    """Read complete gzip members without retaining archive row bodies.

    A compacted archive is deliberately a sequence of closed gzip members: a
    kill can therefore leave only the last member incomplete. ``zlib`` exposes
    the unused bytes after each member, which lets us find the last durable byte
    without loading the archive or trusting ``gzip.open`` to recover from EOF.
    The returned truncation offset is applied only after the caller verifies the
    archive identity, so a foreign archive is never modified.
    """
    header: Optional[dict[str, object]] = None
    archived: dict[int, tuple] = {}
    durable_records = 0
    last_good_offset = 0
    truncated = False
    pending = b""
    with path.open("rb") as raw:
        while True:
            if pending:
                chunk = pending
                pending = b""
            else:
                chunk = raw.read(_COMPACTED_ARCHIVE_READ_CHUNK)
            if not chunk:
                break
            member_header: Optional[dict[str, object]] = None
            member_archived: dict[int, tuple] = {}
            member_error: list[ValueError] = []
            member_records = 0

            def consume_line(raw_line: bytes) -> None:
                nonlocal member_header, member_records
                if member_error:
                    return
                try:
                    value = json.loads(raw_line)
                    if not isinstance(value, dict):
                        raise ValueError("non-object record")
                    member_records += 1
                    if durable_records == 0 and member_records == 1:
                        member_header = value
                        return
                    int_value = value.get("id")
                    if not isinstance(int_value, (int, str)):
                        raise ValueError("invalid message identity")
                    key = (int(int_value), value.get("session_id"), value.get("timestamp"))
                    if key[1] is None or key[2] is None:
                        raise ValueError("invalid message identity")
                    previous = archived.get(key[0], member_archived.get(key[0]))
                    if previous is not None and previous != key:
                        raise ValueError(f"conflicting message identity for id {key[0]}")
                    member_archived[key[0]] = key
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    member_error.append(ValueError(f"invalid JSON or message identity in compacted-history archive {path}: {exc}"))

            pending, member_end, complete = _read_compacted_member(raw, chunk, consume_line)
            if not complete:
                truncated = True
                break
            if member_error:
                raise member_error[0]
            if durable_records == 0:
                if member_records == 0:
                    last_good_offset = member_end
                    continue
                if (member_header is None or member_header.get("_format") != _COMPACTED_ARCHIVE_FORMAT
                        or not isinstance(member_header.get("identity"), dict)):
                    raise ValueError(f"compacted-history archive {path} has no recognized identity header")
                header = cast(dict[str, object], member_header["identity"])
            archived.update(member_archived)
            durable_records += member_records
            last_good_offset = member_end
    if header is None:
        # No complete record exists, so no row could have been deleted. The
        # caller may safely replace this prefix with a fresh durable header.
        return None, {}, True, 0
    return header, archived, truncated, last_good_offset


def _truncate_compacted_archive(path: Path, offset: int) -> None:
    with path.open("r+b") as raw:
        raw.truncate(offset)
        raw.flush()
        _sync_fd(raw.fileno())
    _fsync_directory(path.parent)


def _append_compacted_archive_member(path: Path, records: list[dict[str, object]], *, parent_created: bool = False) -> None:
    """Append one complete gzip member, then make both file and first directory entry durable."""
    first_creation = not path.exists()
    with path.open("ab") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            for record in records:
                gz.write((json.dumps(record, default=str, separators=(",", ":")) + "\n").encode("utf-8"))
        raw.flush()
        _sync_fd(raw.fileno())
    if first_creation or parent_created:
        _fsync_directory(path.parent)


def _compacted_archive_identity(db) -> dict[str, object]:
    """Stable identity for the opened DB, including the per-store UUID when available."""
    identity: dict[str, object] = {"db_path": str(Path(db.db_path).expanduser().resolve())}
    meta = db._read_one("SELECT value FROM state_meta WHERE key = 'store_instance_id'")
    if meta is not None and meta[0]:
        identity["store_instance_id"] = str(meta[0])
        return identity
    # Old stores without provenance still get a stable identity from their first
    # creation row rather than relying on collidable message ids alone.
    first = db._read_one("SELECT id, started_at FROM sessions ORDER BY rowid LIMIT 1")
    if first is not None:
        identity["first_session"] = {"id": str(first[0]), "started_at": first[1]}  # type: ignore[assignment]
    return identity


def _message_archive_key(row) -> tuple:
    return (int(row["id"]), row["session_id"], row["timestamp"])


class SessionMaintenanceMixin:
    """Retention pruning, stale-session archiving and VACUUM policy for SessionDB."""

    def prune_empty_ghost_sessions(self, sessions_dir: Optional[Path] = None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400
        def _do(conn):
            ids = [r[0] for r in conn.execute("""
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """, (cutoff,)).fetchall()]
            from hermes_state_mutation_retirement import retire_prunable
            ids = retire_prunable(conn, ids)
            for chunk in _id_chunks(ids):
                conn.execute(f"DELETE FROM sessions WHERE id IN ({_placeholders(chunk)})", chunk)
            if ids:
                self._delete_unreferenced_system_prompts(conn)
            return ids
        removed_ids = self._execute_write(_do, skip_result=0) or []
        emit_session_deleted(self, removed_ids, reason="ghost_prune", sessions_dir=sessions_dir)
        from hermes_state_media import collect_retired_media
        collect_retired_media(self)
        for sid in removed_ids if sessions_dir else ():
            self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def _guarded_ids(self, conn, ids: Iterable[str]) -> set:
        """Ids in *ids* protected by a live turn lease / compression lock. Idle compression-ended
        parents are closed, not live, so they are not guarded (prune and delete share this)."""
        return {sid for sid in ids if self._write_guards_reject(conn, sid, allow_closed_compression_parent=True)}

    def _holds_guard_readonly(self, conn, sid: str) -> bool:
        """True when ``sid`` has a compression lock or turn lease whose holder is not provably dead.
        Never deletes: expiry alone is not proof the holder is gone (a starved refresher renews it)."""
        from hermes_state import _compression_lock_holder_process_is_dead
        rows = [conn.execute("SELECT holder FROM compression_locks WHERE session_id = ?", (sid,)).fetchone(),
                conn.execute("SELECT holder FROM session_turn_leases WHERE conversation_id = ?",
                             (self._session_turn_lease_key_on_conn(conn, sid),)).fetchone()]
        return any(row is not None and not _compression_lock_holder_process_is_dead(row["holder"]) for row in rows)

    def _write_guards_reject(self, conn, sid: str, **kwargs) -> bool:
        """True when a live turn lease / compression lock protects ``sid``; expired or
        dead-holder guards are reclaimed and fenced as a side effect."""
        from hermes_state import SessionCompressionInProgressError
        from hermes_state_errors import SessionTurnLeaseLostError
        try:
            self._check_transcript_write_guards(
                conn, sid, compression_lock_holder=None, turn_lease_holder=None,
                reject_active_turn_lease=True, reject_active_compression_lock=True, **kwargs)
        except (SessionCompressionInProgressError, SessionTurnLeaseLostError):
            return True
        return False

    def sweep_orphaned_sessions(
        self, *, max_idle_seconds: float, sources: tuple[str, ...] = ("tui", "desktop", "subagent"),
        exclude_ids: tuple[str, ...] = (), exclude_pinned: bool = False,
        heartbeat_staleness_seconds: Optional[float] = None,
        heartbeat_ownership_grace_seconds: Optional[float] = None, respect_gateway_heartbeats: bool = True,
    ) -> list[str]:
        """Close session rows orphaned by a dead gateway process (its in-process disconnect grace timer died
        with it, leaving ``ended_at IS NULL`` forever).  Rows of ``sources`` whose ``started_at`` AND
        canonical last activity are both older than ``max_idle_seconds`` get
        ``end_reason='startup_orphan_reap'`` (the ``started_at`` predicate protects fresh compression/branch
        children whose copied activity is old).  Only pass sources whose lifecycle the caller owns — never
        messaging platforms like ``telegram`` (ending those triggers a routing loop).  ``exclude_ids`` spares
        rows this process still holds.  Non-destructive: messages kept, row resumable, first-reason-wins.
        With ``respect_gateway_heartbeats`` a row is reaped only when no live backend (heartbeat within
        ``heartbeat_staleness_seconds``, default ``2 * max_idle_seconds``) could own it: B owns S if
        ``B.started_at <= S.started_at + grace`` (default = staleness) — grace covers a migrating backend
        whose sessions predate its first heartbeat, bounded so a PID-reuse respawn cannot protect rows
        forever.  Disable the gate only for state.db-owned sources.  SELECT, live-lease validation and UPDATE
        run in one ``BEGIN IMMEDIATE`` transaction; active leases/locks spare the row, expired guards are
        removed so their owner is fenced.

        See #65194.
        ``exclude_pinned`` is intended for broad automatic sweeps; pinned rows remain explicitly
        recoverable. See #60609.
        """
        srcs = tuple(s for s in sources if s)
        if max_idle_seconds <= 0 or not srcs:
            return []
        hb_staleness, hb_grace = heartbeat_staleness_seconds, heartbeat_ownership_grace_seconds
        if not (hb_staleness and hb_staleness > 0):
            hb_staleness = max_idle_seconds * 2
        if not (hb_grace is not None and hb_grace >= 0):
            hb_grace = hb_staleness
        cutoff = (now := time.time()) - max_idle_seconds
        pin_scope = f" AND {_not_pinned_sql('sessions')}" if exclude_pinned else ""
        orphan_predicate = f"started_at < ? AND {_sql_session_last_active('sessions')} < ?"
        heartbeat_params: tuple[float, ...] = ()
        if respect_gateway_heartbeats:
            orphan_predicate += (" AND NOT EXISTS (SELECT 1 FROM gateway_heartbeats h WHERE"
                                 " h.last_heartbeat >= ? AND h.started_at <= sessions.started_at + ?)")
            heartbeat_params = (now - hb_staleness, hb_grace)
        scope_sql = f" AND source IN ({_placeholders(srcs)}){pin_scope} AND {orphan_predicate}"
        scope_params = (*srcs, cutoff, cutoff, *heartbeat_params)
        def _do(conn):
            rows = conn.execute(f"SELECT id FROM sessions WHERE ended_at IS NULL{scope_sql}",
                                scope_params).fetchall()
            excluded = {str(x) for x in exclude_ids if x}
            victims = [sid for sid in (str(row["id"]) for row in rows)
                       if sid not in excluded and not self._write_guards_reject(conn, sid)]
            if not victims:
                return []
            # Re-apply every predicate under the write lock.
            conn.execute(
                f"UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap'"
                f" WHERE id IN ({_placeholders(victims)}) AND ended_at IS NULL{scope_sql}",
                (time.time(), *victims, *scope_params))
            return victims
        return self._execute_write(_do, skip_result=[]) or []

    @staticmethod
    def _prune_filter_where(*, archived: Optional[bool] = None, include_pinned: bool = False,
                            lineage_tips_only: bool = False, include_open: bool = False, **filters) -> tuple[str, list]:
        """Shared WHERE clause for bulk prune/archive selection (alias ``s``): ``_PRUNE_FILTERS``
        AND together, only ended sessions are ever candidates, ``archived`` is tri-state
        (None = both), ``*_like`` are case-insensitive substrings, the rest exact.
        ``lineage_tips_only`` (bulk archive) drops compression ancestors: they are archived with
        their tip, never on their own age — matching an old ancestor would fan out over the lineage
        and hide its OPEN, recently active tip (#115489) — and the canonical Bot Chat (the idle
        sweep's exemption). ``include_open`` lifts the ended guard
        for reversible archive only — never for prune/delete (#139597: desktop sessions never set
        ``ended_at``)."""
        unknown = set(filters) - _PRUNE_FILTER_NAMES
        if unknown:
            raise TypeError("SessionMaintenanceMixin._prune_filter_where() got an unexpected "
                            f"keyword argument {min(unknown)!r}")
        clauses = [] if include_open else ["s.ended_at IS NOT NULL"]
        clauses.append(visible_session_sql("s.id"))
        if lineage_tips_only:
            clauses.append("COALESCE(s.end_reason, '') <> 'compression'")
            # Bulk archive is housekeeping, like the idle sweep: it never retires the canonical
            # Bot Chat (archiving releases its name, #139779). Only a per-chat Archive may. Matched by
            # title alone: compression moves the title onto a VISIBLE continuation tip.
            clauses.append("COALESCE(s.title, '') <> 'Bot Chat'")
        params: list = []
        for name, applies, build in _PRUNE_FILTERS:
            value = filters.get(name)
            if (value is not None) if applies == "notnone" else bool(value):
                new_clauses, new_params = build(value)
                clauses.extend(new_clauses)
                params.extend(new_params)
        if isinstance(archived, bool):
            clauses.append(f"s.archived = {int(archived)}")
        # Pinned is a durable "keep" flag: bulk prune/delete/archive exclude pinned rows unless opted in.
        if not include_pinned:
            clauses.append(_not_pinned_sql())
        return " AND ".join(clauses), params

    def _prune_where(self, older_than_days, source, filters, *, whole_lineages: bool = False) -> tuple[str, list]:
        """Translate the legacy age window into the shared activity filter, then build WHERE.
        ``whole_lineages`` (prune) keeps a compression ancestor while any continuation after it
        is unmatched."""
        if (older_than_days is not None and filters.get("last_active_before") is None
                and filters.get("started_before") is None):
            if older_than_days < 0:
                raise ValueError(
                    f"older_than_days must be >= 0, got {older_than_days!r}: a negative "
                    "retention builds a future cutoff that matches every ended session.")
            filters["last_active_before"] = time.time() - (older_than_days * 86400)
        where, params = self._prune_filter_where(source=source, **filters)
        if not whole_lineages:
            return where, params
        # A compressed-away segment ages with its conversation, not on its own: while any later
        # segment stays, deleting it would cut the start off a chat that is still in use.
        return f"{where} AND s.id NOT IN ({_continued_ancestors_sql(where)})", [*params, *params]

    def list_prune_candidates(self, older_than_days: Optional[float] = None, source: str | None = None, *,
                              whole_lineages: bool = False, **filters) -> list[dict[str, Any]]:
        """Dry-run: sessions a matching prune/archive would touch, oldest first (``older_than_days``
        = inactivity threshold: freshest of ``last_activity_at`` / latest message / ``started_at``)."""
        where, params = self._prune_where(older_than_days, source, filters, whole_lineages=whole_lineages)
        return [dict(row) for row in self._read_all(
            f"""SELECT s.id, s.source, s.title, s.model, s.started_at,
                           {_LAST_ACTIVE_SQL} AS last_active,
                           s.ended_at, s.message_count, s.archived
                    FROM sessions s WHERE {where}
                    ORDER BY last_active ASC, s.started_at ASC""", params)]

    def count_prune_matches(self, older_than_days: Optional[float] = None, source: str | None = None, *,
                            pinned_only: bool = False, **filters) -> int:
        """Count-only :meth:`list_prune_candidates`; ``pinned_only`` counts rows carrying the pin
        itself, not the continuations it protects (CLI reports spared pinned sessions)."""
        if pinned_only:
            filters["include_pinned"] = True
        where, params = self._prune_where(older_than_days, source, filters)
        if pinned_only:
            where += " AND COALESCE(s.pinned, 0) = 1"
        return int(self._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {where}", params)[0])

    def count_open_prune_matches(self, older_than_days: Optional[float] = None, source: str | None = None,
                                 **filters) -> int:
        """Count open sessions a matching prune skips (``ended_at`` guard inverted); visibility-only."""
        if filters.get("include_open"):
            return 0
        where, params = self._prune_where(older_than_days, source, filters)
        ended_guard = "s.ended_at IS NOT NULL"
        if not where.startswith(ended_guard):
            raise RuntimeError("prune filter lost its ended-session safety guard")
        open_where = f"s.ended_at IS NULL{where[len(ended_guard):]}"
        return int(self._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {open_where}", params)[0])

    def archive_stale_sessions(self, idle_days: float, *, exclude_pinned: bool = True) -> int:
        """Archive every session untouched for ``idle_days`` (freshest of ``last_activity_at`` /
        latest message / ``started_at``); may archive unended sessions.  ``archived = 0`` makes
        repeats no-ops; only lineage tips (``end_reason <> 'compression'``) are candidates — a
        stale tip archives its chain via :meth:`set_session_archived`, so an old compressed-away
        root with a recent continuation is never matched.  The hidden canonical Bot Chat (same
        predicate as :meth:`set_session_pinned`) is exempt: only a deliberate archive may retire
        it, since archiving releases its registry title to the next Bot open."""
        if idle_days is None or idle_days < 0:
            return 0
        cutoff = time.time() - float(idle_days) * 86400.0
        pin_clause = f"AND {_not_pinned_sql()}" if exclude_pinned else ""
        rows = self._read_all(
            f"""
            SELECT s.id FROM sessions s
            WHERE s.archived = 0
              AND COALESCE(s.end_reason, '') <> 'compression'
              {pin_clause}
              AND NOT (COALESCE(s.hidden, 0) <> 0 AND COALESCE(s.title, '') = ?)
              AND {_LAST_ACTIVE_SQL} < ?
            ORDER BY s.started_at ASC
            """, (self.CANONICAL_BOT_CHAT_TITLE, cutoff))
        for row in rows:
            # Sweep provenance: a later compression/resume of this lineage un-hides it (#117713).
            self._auto_archive_lineage(row[0])
        return len(rows)

    def _prune_conversation_groups(self, candidate_ids: list[str]) -> list[list[str]]:
        """*candidate_ids* in order, each local reset/compression conversation's candidates moved
        together to its first one: ``retire_prunable`` sweeps such a conversation only when every
        segment is in the same transaction, so a batch boundary between them would keep it forever
        once the pacer shrinks batches."""
        from hermes_state_local import local_conversation_ids
        from hermes_state_prune_budget import prune_sql, check_prune_budget
        groups: dict[str, list[str]] = {}
        with self._read_ctx() as conn, prune_sql(conn):
            for sid in candidate_ids:
                check_prune_budget()
                groups.setdefault(local_conversation_ids(conn, sid)[0], []).append(sid)
        return list(groups.values())

    def compacted_history_retention(self, older_than_days: float = 14, archive_path: Optional[Path] = None,
                                    dry_run: bool = False, batch_size: int = 500) -> dict:
        """Archive and remove superseded message rows without touching active history.

        The archive is identity-bound JSONL gzip. Each append is a complete gzip
        member, followed by file fsync, and only then a separate committed DELETE
        transaction. A crash can therefore leave an archived-but-still-present row
        (harmless and recovered on the next run), but never a deleted row without
        a durable archive record. The DELETE rechecks the age and inactive
        predicates under its write lock; rows changed concurrently remain in both
        places until a later pass.
        """
        if older_than_days < 0 or batch_size < 1:
            raise ValueError("older_than_days must be non-negative and batch_size must be positive")
        cutoff = time.time() - older_than_days * 86400.0
        archive = Path(archive_path or
                       "/Volumes/Hermes Archive/compacted-history.jsonl.gz").expanduser()
        where = "active = 0 AND timestamp < ?"
        count, bytes_est = self._read_one(
            f"SELECT COUNT(*), COALESCE(SUM(length(COALESCE(content, ''))), 0) FROM messages WHERE {where}", (cutoff,))
        result = {"rows": int(count), "estimated_bytes": int(bytes_est), "archive": str(archive),
                  "deleted": 0, "recovered_truncated_member": False}
        if dry_run:
            return result

        identity = _compacted_archive_identity(self)
        parent_created = not archive.parent.exists()
        archive.parent.mkdir(parents=True, exist_ok=True)
        archived_by_id: dict[int, tuple] = {}
        truncate_offset: Optional[int] = None
        if archive.exists():
            archive_identity, archived_by_id, recovered, truncate_offset = _read_compacted_archive(archive)
            result["recovered_truncated_member"] = recovered
            # An empty or torn-first-member archive contains no durable header or
            # deleted rows; replace it with a fresh header on this run.
            if archive_identity is None:
                truncate_offset = 0
            elif archive_identity != identity:
                # Identity is checked before applying any repair write. In
                # particular, a foreign archive with a torn tail is untouched.
                raise ValueError(
                    f"refusing compacted-history archive for a different database: {archive} "
                    f"(archive={archive_identity!r}, current={identity!r})"
                )
            if truncate_offset is not None:
                _truncate_compacted_archive(archive, truncate_offset)
                if archive_identity is None:
                    _append_compacted_archive_member(
                        archive, [{"_format": _COMPACTED_ARCHIVE_FORMAT, "identity": identity}]
                    )
                elif recovered:
                    logger.warning("ignored and truncated incomplete trailing gzip member in %s", archive)
        else:
            _append_compacted_archive_member(
                archive, [{"_format": _COMPACTED_ARCHIVE_FORMAT, "identity": identity}],
                parent_created=parent_created,
            )

        if not count:
            return result

        while True:
            rows = self._read_all(  # type: ignore[attr-defined]
                f"SELECT * FROM messages WHERE {where} ORDER BY id LIMIT ?", (cutoff, batch_size))
            if not rows:
                break
            ids = [int(row["id"]) for row in rows]
            new_records: list[dict] = []
            for row in rows:
                key = _message_archive_key(row)
                previous = archived_by_id.get(key[0])
                if previous is not None and previous != key:
                    raise ValueError(
                        f"refusing to deduplicate conflicting message identity for id {key[0]} "
                        f"in compacted-history archive {archive}"
                    )
                if previous is None:
                    new_records.append(dict(row))
                    archived_by_id[key[0]] = key
            if new_records:
                # Close the gzip member before fsync and before DELETE. This is
                # intentionally one member per batch, not one member per run.
                _append_compacted_archive_member(archive, new_records)

            def _delete_batch(conn, ids=ids):
                cursor = conn.execute(
                    f"DELETE FROM messages WHERE id IN ({_placeholders(ids)}) "
                    "AND active = 0 AND timestamp < ?",
                    (*ids, cutoff),
                )
                return max(cursor.rowcount, 0)

            deleted = int(self._execute_write(_delete_batch, skip_result=0) or 0)  # type: ignore[attr-defined]
            if deleted == 0:
                raise RuntimeError(
                    f"compacted-history retention made no delete progress for selected batch of {len(ids)} row(s)"
                )
            result["deleted"] += deleted
        return result

    def prune_sessions(self, older_than_days: Optional[float] = 90, source: str | None = None,
                       sessions_dir: Optional[Path] = None, exclude_active_write_guards: bool = False,
                       deleted_ids: Optional[list] = None, within_deletion_window: bool = False,
                       max_seconds: Optional[float] = None,
                       **filters) -> int:
        """Delete ended sessions inactive for ``older_than_days`` (an explicit ``started_before`` /
        ``last_active_before`` overrides it; None = no implicit bound) matching the filters.
        Children outside the window are orphaned (parent NULLed), not cascade-deleted.  With
        *sessions_dir*, transcript files are removed outside the DB transaction.
        ``exclude_active_write_guards`` (automatic maintenance) skips rows under a live turn lease
        or compression lock while expired/dead holders are reclaimed and fenced.  A compression
        ancestor is deleted only together with every continuation after it (``whole_lineages``).
        ``within_deletion_window`` refuses to start outside ``maintenance.deletion_window`` and stops
        inside SQLite VM work and between transactions once it closes. ``max_seconds`` adds a
        runtime cap; a deadline-interrupted page rolls back and resumes next run. One
        pass per store runs at a time in this process (:func:`prune_pass`)."""
        if filters.get("include_open"):
            raise ValueError("prune_sessions() never deletes open sessions: include_open is archive-only")
        from hermes_cli.maintenance_window import deletion_window_open
        if within_deletion_window and not deletion_window_open():
            raise DeletionWindowClosedError("session prune refused: maintenance.deletion_window is closed")
        from hermes_state_prune_budget import PruneBudget, PruneBudgetExpired, PruneWriteContended, prune_budget
        from hermes_state_transactions import finish_prune_maintenance
        removed_ids = []
        db_path: Path = getattr(self, "db_path")
        budget = PruneBudget(
            within_deletion_window, max_seconds, db_path=db_path,
            window_already_admitted=within_deletion_window,
        )
        try:
            with prune_pass(db_path), prune_budget(budget):
                result = self._prune_sessions_pass(older_than_days, source, sessions_dir, exclude_active_write_guards,
                                                   removed_ids, within_deletion_window, filters)
                finish_prune_maintenance(self)
                return result
        except PruneBudgetExpired as exc:
            logger.info("%s", exc)
            return len(removed_ids)
        except PruneWriteContended as exc:
            # A read or a write outside the retry runner can still observe a
            # foreground writer. The next maintenance pass can resume safely.
            logger.info("%s", exc)
            return len(removed_ids)
        finally:
            if deleted_ids is not None:
                deleted_ids.extend(removed_ids)

    def _prune_sessions_pass(self, older_than_days, source, sessions_dir, exclude_active_write_guards,
                             deleted_ids, within_deletion_window, filters) -> int:
        from hermes_cli.maintenance_window import deletion_window_open
        from hermes_state_prune_budget import run_prune_write
        finish_pending_deletes(self)
        where, where_params = self._prune_where(older_than_days, source, filters, whole_lineages=True)
        # Select outside the write transaction.  The old implementation selected and deleted every
        # victim inside one BEGIN IMMEDIATE, so a large FTS cascade could hold every foreground writer
        # out for minutes.  Each short transaction rechecks the full predicate and the active guards;
        # a concurrent change therefore turns into a harmless no-op, and the callback remains
        # idempotent under _execute_write's whole-callback retry contract.
        # Newest first: a continuation starts after its compression ancestor, so it is deleted in an
        # earlier (or the same) batch and a later recheck can never strand it without its start.
        candidate_rows = self._read_all(
            f"SELECT s.id FROM sessions s WHERE {where} ORDER BY s.started_at DESC", where_params)
        candidate_ids = [str(row["id"]) for row in candidate_rows]
        # Tombstones are retained on a separate retry horizon from transcript retention.  A
        # prune pass must advance that horizon even when no session row matches its age window.
        from hermes_state_mutation_retirement import (
            RETIRED_PREFIX, ROUTE_FENCE_PREFIX, prune_retired_tombstones,
        )
        from hermes_state_terminal import ADMISSION_PREFIX, WORKER_PREFIX
        tombstone_pacer = PrunePacer(max_size=100)
        tombstone_prefixes = (ADMISSION_PREFIX, WORKER_PREFIX, RETIRED_PREFIX, ROUTE_FENCE_PREFIX)
        while True:
            unit_limit = [tombstone_pacer.size]

            def prune_tombstones():
                return self._execute_write(
                    lambda conn: prune_retired_tombstones(conn, limit=unit_limit[0]), skip_result={},
                )

            def shrink_tombstones():
                changed = tombstone_pacer.shrink()
                unit_limit[0] = tombstone_pacer.size
                return changed

            started = time.monotonic()
            reclaimed = run_prune_write(prune_tombstones, shrink=shrink_tombstones,
                                        label="retired tombstones", skip_result={}) or {}
            if not reclaimed or all(reclaimed.get(prefix, 0) < unit_limit[0] for prefix in tombstone_prefixes):
                break
            tombstone_pacer.pause(time.monotonic() - started)
        removed_count = 0

        # Keep each transaction small enough that message/FTS triggers release the WAL write lock
        # promptly.  The pause is intentional: without yielding between transactions, a maintenance
        # loop can reacquire the lock often enough to starve a live turn.  The pacer sizes the next
        # batch by this one's measured hold and pauses at least that long.
        groups = self._prune_conversation_groups(candidate_ids)
        pacer, position, held = PrunePacer(max_size=25), 0, [0.0]
        while position < len(groups):
            if within_deletion_window and not deletion_window_open():
                logger.info("session prune stopped: maintenance.deletion_window closed with %d candidate(s) "
                            "left for the next run", sum(map(len, groups[position:])))
                break
            batch_start = position
            requested = []

            def attempt():
                nonlocal requested, position
                position, requested = batch_start, []
                while position < len(groups) and (not requested or len(requested) + len(groups[position]) <= pacer.size):
                    requested += groups[position]
                    position += 1
                return self._execute_write(_do, skip_result=[]) or []

            def shrink():
                return pacer.shrink(len(requested)) if position - batch_start > 1 else False

            def _do(conn):
                started = time.monotonic()
                try:
                    return _batch(conn, requested)
                finally:
                    held[0] = time.monotonic() - started

            def _select(conn, requested):
                ph = _placeholders(requested)
                rows = conn.execute(
                    f"SELECT s.id FROM sessions s WHERE {where} AND s.id IN ({ph})",
                    (*where_params, *requested),
                ).fetchall()
                session_ids = [str(row["id"]) for row in rows]
                if exclude_active_write_guards:
                    # UNION, not UNION ALL: a parent_session_id cycle would otherwise recurse
                    # forever while this transaction holds the write lock.
                    lineage_rows = conn.execute(
                        f"WITH RECURSIVE lineage(root, id) AS ("
                        f"SELECT id, id FROM sessions WHERE id IN ({ph}) "
                        f"UNION SELECT lineage.root, child.id FROM sessions child "
                        f"JOIN lineage ON child.parent_session_id = lineage.id) "
                        f"SELECT root, id FROM lineage",
                        requested,
                    ).fetchall()
                    # Reclaim stale guards only on rows this transaction may delete (the victims, as
                    # before batching). Descendants outside the batch are probed read-only: an expired
                    # lease can still belong to a live holder whose refresher was starved.
                    victims = set(session_ids)
                    guarded = self._guarded_ids(conn, victims) | {
                        str(row["id"]) for row in lineage_rows
                        if str(row["id"]) not in victims and self._holds_guard_readonly(conn, str(row["id"]))
                    }
                    guarded_roots = {
                        str(row["root"]) for row in lineage_rows if str(row["id"]) in guarded
                    }
                    session_ids = [sid for sid in session_ids if sid not in guarded_roots]
                return session_ids

            def _batch(conn, requested):
                from hermes_state_mutation_retirement import retire_prunable
                session_ids = _select(conn, requested)
                session_ids = retire_prunable(conn, sorted(session_ids), defer_messages=True)
                if not session_ids:
                    return []
                stage_delete(conn, session_ids, sessions_dir)
                return session_ids

            def defer():
                from hermes_state_deletion import defer_retirement
                return defer_retirement(self, lambda conn: _select(conn, requested), sessions_dir,
                                        label=lambda: requested or groups[batch_start])

            batch_removed = run_prune_write(attempt, shrink=shrink, defer=defer,
                                             label=lambda: requested or groups[batch_start]) or []
            removed_count += len(batch_removed)
            deleted_ids.extend(batch_removed)
            if batch_removed:
                emit_session_deleted(self, batch_removed, reason="prune", sessions_dir=sessions_dir)
            finish_pending_deletes(self, required_ids=batch_removed)
            # finish_pending_deletes removes transcript files before clearing its marker.
            if position < len(groups):
                pacer.pause(held[0])

        if removed_count:
            while True:
                deleted_prompts = self._execute_write(
                    lambda conn: self._delete_unreferenced_system_prompts(conn, limit=100), skip_result=0)
                if not deleted_prompts or deleted_prompts < 100:
                    break
        from hermes_state_media import collect_retired_media_bounded
        collect_retired_media_bounded(self)
        return removed_count

    def maybe_auto_archive(
        self, idle_days: float = 3, min_interval_hours: int = 24, exclude_pinned: bool = True,
    ) -> dict[str, Any]:
        """Idempotent, non-destructive auto-archive of sessions idle for ``idle_days``; state_meta
        ``last_auto_archive`` gates runs within ``min_interval_hours``. Never raises."""
        result: dict[str, Any] = {"skipped": False, "archived": 0}
        try:
            now = time.time()
            try:
                last = float(self.get_meta("last_auto_archive") or 0.0)
            except (TypeError, ValueError):
                last = 0.0  # corrupt meta; treat as no prior run
            if last and now - last < min_interval_hours * 3600:
                result["skipped"] = True
                return result
            # Startup-watchdog lease: the archive sweep is I/O-bound (near-zero CPU),
            # which the watchdog's CPU fallback misreads as a parked deadlock.
            # No-op when the watchdog is not armed; never raises.
            report_startup_progress(900.0, phase="state_db_auto_archive")
            archived = result["archived"] = self.archive_stale_sessions(idle_days, exclude_pinned=exclude_pinned)
            # Record even a zero-archive run so we don't re-sweep every call.
            self.set_meta("last_auto_archive", str(now))
            if archived > 0:
                logger.info(
                    "state.db auto-archive: archived %d session(s) idle >= %s days", archived, idle_days,
                )
        except Exception as exc:
            logger.warning("state.db auto-archive failed: %s", exc)
            result["error"] = str(exc)
        return result

    def _page_pragmas(self, names: tuple[str, ...], fail_msg: str) -> Optional[list]:
        """Integer PRAGMAs over the existing connection (never a byte probe); None + debug log on failure."""
        try:
            with self._read_ctx() as conn:
                if self._conn is None:
                    return None
                return [int(conn.execute(f"PRAGMA {name}").fetchone()[0]) for name in names]
        except Exception as exc:
            logger.debug(fail_msg, exc)
            return None

    def logical_size_bytes(self) -> Optional[int]:
        """``page_count * page_size``: main-file size once the WAL is checkpointed in.  Prefer
        over ``os.path.getsize`` when reporting a VACUUM: in WAL mode the rewrite lands in
        ``-wal`` and the checkpoint is refused while another connection holds a read-mark, so
        a stat() delta understates the win and can go negative."""
        values = self._page_pragmas(("page_count", "page_size"), "Could not read logical DB size: %s")
        return None if values is None else values[0] * values[1]

    def _freelist_ratio(self) -> Optional[float]:
        """Reclaimable fraction (``freelist_count / page_count``) gating VACUUM in
        :meth:`maybe_auto_prune_and_vacuum`; None = fall back to the time throttle.

        ``PRAGMA freelist_count / PRAGMA page_count`` read over the existing connection (never a byte-level
        probe of the live file — see ``sqlite_safe_read``). This is what VACUUM would actually give back; it
        is the gate :meth:`maybe_auto_prune_and_vacuum` uses to decide whether a full rewrite pays off
        (#54189).
        """
        values = self._page_pragmas(("page_count", "freelist_count"), "Could not read freelist ratio: %s")
        return None if values is None else (values[1] / values[0] if values[0] > 0 else 0.0)

    def _try_checkpoint(self, mode: str, fail_msg: str) -> None:
        try:
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
        except Exception as exc:
            logger.debug(fail_msg, exc)

    def vacuum(self) -> int:
        """VACUUM to reclaim space after large deletes (SQLite never shrinks on its own).
        Takes an exclusive lock — callers must ensure no other writers are active.  FTS5
        segments are merged first (:meth:`optimize_fts`) so their pages are reclaimed too;
        returns the number of FTS indexes optimized (0 on merge failure / no FTS). A quarantined
        handle (corrupt image, replaced file, lost WAL generation) raises before any rewrite: a
        VACUUM reads every page and commits the result back, turning contained damage into an
        amplified one (#105670). Same guard ``_execute_write`` applies to every write."""
        self._raise_if_db_corrupt()
        optimized = 0
        try:
            optimized = self.optimize_fts()  # manages its own lock
        except Exception as exc:
            logger.warning("FTS optimize before VACUUM failed: %s", exc)
        with self._lock:
            self._raise_if_db_replaced()
            if self._conn is None:
                self._reopen_after_close_locked(context="write")
            # PASSIVE, not TRUNCATE: a manual `hermes sessions vacuum` runs in a transient CLI
            # process; a TRUNCATE reset here would race a live gateway writer.
            self._try_checkpoint("PASSIVE", "WAL checkpoint (PASSIVE) before VACUUM failed: %s")
            self._conn.execute("VACUUM")
            # VACUUM rewrites every page THROUGH the WAL; without this TRUNCATE a 3 GB DB leaves a 3 GB -wal.
            self._try_checkpoint("TRUNCATE", "WAL checkpoint (TRUNCATE) after VACUUM failed: %s")
            # TRUNCATE may replace the WAL inode; adopt the new sidecars so the
            # write-path generation guard does not halt this connection.
            self._record_db_file_identity()
        return optimized

    def maybe_auto_prune_and_vacuum(
        self, retention_days: int = 90, min_interval_hours: int = 24, vacuum: bool = True,
        sessions_dir: Optional[Path] = None, min_vacuum_interval_days: int = 30,
        min_vacuum_freelist_ratio: float = AUTO_VACUUM_MIN_FREELIST_RATIO,
    ) -> dict[str, Any]:
        """Idempotent startup auto-maintenance (never raises): prune inactive sessions, reap stale open
        state-owned rows, optional VACUUM.  Runs at most once per ``min_interval_hours``; VACUUM has its own
        ``min_vacuum_interval_days`` throttle and also requires ``freelist_count / page_count`` >
        ``min_vacuum_freelist_ratio`` so a small prune on a dense multi-GB database never triggers a full
        rewrite.  Stale-open reconciliation: cron/kanban/subagent/one-shot CLI rows never set ``ended_at``
        when their process dies and prune only deletes ended rows, so after pruning, open rows from
        :attr:`_AUTO_PRUNE_STALE_OPEN_SOURCES` older than ``retention_days`` are closed
        (``startup_orphan_reap``); they stay resumable and age from their close.  Returns ``{"skipped",
        "pruned", "closed", "vacuumed"}`` plus ``"freelist_ratio"`` when a VACUUM was considered and
        ``"error"`` on failure.

        Records the last run timestamp in state_meta so subsequent calls within ``min_interval_hours``
        no-op. Designed to be called once at startup from long-lived entrypoints (CLI, gateway, cron
        scheduler). See #54189.
        When *sessions_dir* is provided, on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for pruned sessions are removed as part of the same sweep (issue #3015).
        Messaging and UI sources are never touched here. See #54189.

        The active profile's ``maintenance.deletion_window`` is an invariant of
        automatic pruning, not only a caller convention. Calls outside that
        window return before acquiring the maintenance lock or spending the
        24-hour throttle.
        """
        from hermes_state_repair import _release_auto_maintenance_lock, _try_acquire_auto_maintenance_lock
        result: dict[str, Any] = {"skipped": False, "pruned": 0, "closed": 0, "vacuumed": False}
        if retention_days is None or retention_days < 0:
            # A negative retention would build a future cutoff and match every ended
            # session; auto_prune=false is the disable switch, not a negative bound.
            logger.warning(
                "state.db auto-maintenance skipped: sessions.retention_days=%r is outside the allowed "
                "range (a whole number of days >= 0); set sessions.auto_prune: false to disable pruning",
                retention_days)
            result["skipped"] = True
            return result
        from hermes_cli.maintenance_window import deletion_window_open
        if not deletion_window_open():
            logger.debug("state.db auto-maintenance skipped: outside maintenance.deletion_window")
            result["skipped"] = True
            return result
        maintenance_lock = _try_acquire_auto_maintenance_lock(self.db_path)
        if maintenance_lock is None:
            result["skipped"] = True
            return result
        try:
            now = time.time()
            since_prune = _seconds_since(now, self.get_meta("last_auto_prune"))
            if since_prune is not None and since_prune < min_interval_hours * 3600:
                result["skipped"] = True
                return result
            # Prune first: orphans closed below get a full retention window.
            # Startup-watchdog leases: each long step is I/O-bound (near-zero CPU), which the
            # watchdog's CPU fallback misreads as a parked deadlock. Leases are clamped to
            # _MAX_LEASE_S=900 per call, so a multi-minute step renews per step rather than
            # once at entry. No-op when the watchdog is not armed; never raises.
            report_startup_progress(900.0, phase="state_db_auto_prune")
            result["pruned"] = pruned = self.prune_sessions(
                older_than_days=retention_days, sessions_dir=sessions_dir, exclude_active_write_guards=True,
                within_deletion_window=True)
            if not deletion_window_open():
                result["deadline_reached"] = True
                return result
            report_startup_progress(900.0, phase="state_db_auto_sweep")
            closed = self.sweep_orphaned_sessions(
                max_idle_seconds=float(retention_days) * 86400.0,
                sources=self._AUTO_PRUNE_STALE_OPEN_SOURCES, exclude_pinned=True,
                respect_gateway_heartbeats=False,  # state-owned lifecycles, not gateway heartbeats
            )
            result["closed"] = len(closed)
            # VACUUM only if rows were freed, the time throttle passed AND the
            # freelist ratio passed — it holds an exclusive lock for a full rewrite.
            since_vacuum = _seconds_since(now, self.get_meta("last_vacuum"))
            vacuum_due = since_vacuum is None or since_vacuum >= min_vacuum_interval_days * 86400
            # A pass that ran to the end of the window must not start a full rewrite after it.
            if vacuum and pruned > 0 and vacuum_due and deletion_window_open():
                result["freelist_ratio"] = ratio = self._freelist_ratio()
                # Same admission `hermes sessions optimize` runs: VACUUM plus the TRUNCATE checkpoint
                # retire the WAL generation a sibling writer (gateway, Desktop, dashboard, cron) still
                # holds, and that is exactly the state every agent then refuses turns in (#110054).
                # The foreign scan skips our own pid, so it is paired with the in-process arm: under
                # one multiplexed process another live SessionDB generation for this same path is
                # just as much a holder as another process would be.
                # Automatic maintenance only ever SKIPS — a turn is never refused over housekeeping.
                from hermes_state_holders import (
                    foreign_state_db_holders, in_process_state_db_holders)
                holders = (foreign_state_db_holders(self.db_path)
                           + in_process_state_db_holders(self.db_path, exclude=self))
                if holders:
                    result["vacuum_skipped_holders"] = len(holders)
                    logger.debug(
                        "state.db auto-maintenance: skipping VACUUM, %d other process(es) hold the "
                        "store or a WAL sidecar (%s)",
                        len(holders), ", ".join(f"{pid}:{target}" for pid, target in holders[:3]))
                elif ratio is None or ratio > min_vacuum_freelist_ratio:
                    try:
                        # VACUUM rewrites every page with ~zero CPU: renew the lease here so
                        # a multi-minute rewrite on a large state.db never outlives the clamp.
                        report_startup_progress(900.0, phase="state_db_auto_vacuum")
                        self.vacuum()
                        result["vacuumed"] = True
                        self.set_meta("last_vacuum", str(now))
                    except Exception as exc:
                        logger.warning("state.db VACUUM failed: %s", exc)
                else:
                    logger.debug("state.db auto-maintenance: skipping VACUUM, only "
                                 "%.1f%% of pages reclaimable (threshold %.0f%%)",
                                 ratio * 100.0, min_vacuum_freelist_ratio * 100.0)
            # Record even when pruned == 0 so the throttle holds.
            self.set_meta("last_auto_prune", str(now))
            if closed or pruned > 0:
                logger.info("state.db auto-maintenance: closed %d stale open session(s), "
                            "pruned %d session(s) inactive for %d days%s",
                            len(closed), pruned, retention_days, " + VACUUM" if result["vacuumed"] else "")
        except Exception as exc:
            # Maintenance must never block startup.
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)
        finally:
            _release_auto_maintenance_lock(maintenance_lock)
        return result
