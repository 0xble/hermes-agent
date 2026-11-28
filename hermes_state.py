"""Compatibility module for outside plugins that still import ``hermes_state``; removed in v0.27.0.

Session storage sits behind the storage interface (``persistence`` and ``persistence_assembly``). This module forwards
the names the plugin guide lists under "``hermes_state`` deprecated: use the storage interface" to that interface and
holds no SQL, database connection or state of its own. The first use of any of its names emits one
``DeprecationWarning`` per process naming that name's replacement and the removal release. Every other public name
raises ``AttributeError`` pointing at the guide, so the module's own helpers are underscore names.
"""

import asyncio as _asyncio
import functools as _functools
import importlib as _importlib
import json as _json
import logging as _logging
import os as _os
import queue as _queue
import threading as _threading
import typing as _typing
import warnings as _warnings
import weakref as _weakref
from contextlib import ExitStack as _ExitStack
from pathlib import Path as _Path

if _typing.TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Optional

_REMOVAL_RELEASE = "v0.27.0"
_MIGRATION_GUIDE = (
    "https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#hermes_state-deprecated-use-the-storage-interface"
)

_logger = _logging.getLogger("hermes_state")
_warned = False

# The SQLite backend's writer and read-pool helpers this module carries have no public replacement.
_NO_WRITER_REPLACEMENT = "the store's own write verbs (the SQLite backend's writer helpers have no public replacement)"
_NO_READ_POOL_REPLACEMENT = "the store's own read verbs (the SQLite backend's read pool has no public replacement)"

# Each carried name with the replacement its deprecation warning names.
_REPLACEMENTS = {
    "SessionDB": "persistence_assembly.open_persistence(profile_scope()), or open_persistence_reader(...) for read_only",
    "AsyncSessionDB": "the store handle's aio attribute",
    "DEFAULT_DB_PATH": "persistence_assembly.store_location(profile_scope())",
    "StateDbCorruptError": "persistence.StoreQuarantined",
    "StateDbReplacedError": "persistence.StoreReplaced",
    "SessionCompressionInProgressError": "persistence.CompressionInProgress",
    "DeletedWalGenerationError": "persistence.DeletedWalGeneration",
    "SessionResumeTooLargeError": "persistence.conversation.messages.ResumeTooLarge",
    "SessionExportTooLargeError": "persistence.ExportTooLarge",
    "classify_persistence_error": "storage_errors.classify_persistence_error",
    "is_sqlite_lock_error": "storage_errors.is_sqlite_lock_error",
    "is_malformed_db_error": "storage_errors.is_malformed_db_error",
    "apply_wal_with_fallback": "sqlite_support.apply_wal_with_fallback",
    "apply_database_pragmas": "sqlite_support.apply_database_pragmas",
    "storage_state": "persistence_assembly.storage_health(profile_scope()).storage_state()",
    "storage_corrupt_reason": "persistence_assembly.storage_health(profile_scope()).storage_state()",
    "get_hermes_home": "hermes_constants.get_hermes_home",
    "mkdir_under_hermes_home": "hermes_constants.mkdir_under_hermes_home",
    "divert_session_transcript_jsonl": "session_rescue.divert_session_transcript_jsonl(..., sessions_dir=...)",
    "format_session_db_unavailable": "storage_user_copy.format_session_db_unavailable",
    "get_last_init_error": "storage_user_copy.get_last_init_error",
    "holder_pid_checkable": "process_identity.holder_pid_checkable",
    "make_user_dir": "hermes_dir_acl.make_user_dir",
    "preflight_db_writability": "sqlite_support.preflight_db_writability",
    "read_only_db_uri": "sqlite_support.read_only_db_uri",
    "resolved_max_export_messages": "storage_limits.resolved_max_export_messages",
    "resolved_max_resume_messages": "storage_limits.resolved_max_resume_messages",
    "WriterMutexTimeout": _NO_WRITER_REPLACEMENT,
    "maintain_after_commit": _NO_WRITER_REPLACEMENT,
    "write_site": _NO_WRITER_REPLACEMENT,
    "writer_mutex_until": _NO_WRITER_REPLACEMENT,
    "note_read_pool_exhausted": _NO_READ_POOL_REPLACEMENT,
}

# Names that are another module's object, under a new name or the same one.
_FORWARDED = {
    "StateDbCorruptError": ("persistence", "StoreQuarantined"),
    "StateDbReplacedError": ("persistence", "StoreReplaced"),
    "SessionCompressionInProgressError": ("persistence", "CompressionInProgress"),
    "DeletedWalGenerationError": ("persistence", "DeletedWalGeneration"),
    "SessionResumeTooLargeError": ("persistence.conversation.messages", "ResumeTooLarge"),
    "SessionExportTooLargeError": ("persistence", "ExportTooLarge"),
    "classify_persistence_error": ("storage_errors", "classify_persistence_error"),
    "is_sqlite_lock_error": ("storage_errors", "is_sqlite_lock_error"),
    "is_malformed_db_error": ("storage_errors", "is_malformed_db_error"),
    "apply_wal_with_fallback": ("sqlite_support", "apply_wal_with_fallback"),
    "apply_database_pragmas": ("sqlite_support", "apply_database_pragmas"),
    "get_hermes_home": ("hermes_constants", "get_hermes_home"),
    "mkdir_under_hermes_home": ("hermes_constants", "mkdir_under_hermes_home"),
    "format_session_db_unavailable": ("storage_user_copy", "format_session_db_unavailable"),
    "get_last_init_error": ("storage_user_copy", "get_last_init_error"),
    "holder_pid_checkable": ("process_identity", "holder_pid_checkable"),
    "make_user_dir": ("hermes_dir_acl", "make_user_dir"),
    "preflight_db_writability": ("sqlite_support", "preflight_db_writability"),
    "read_only_db_uri": ("sqlite_support", "read_only_db_uri"),
    "resolved_max_export_messages": ("storage_limits", "resolved_max_export_messages"),
    "resolved_max_resume_messages": ("storage_limits", "resolved_max_resume_messages"),
    "WriterMutexTimeout": ("persistence.sqlite._state.write", "WriterMutexTimeout"),
    "maintain_after_commit": ("persistence.sqlite._state.write", "maintain_after_commit"),
    "write_site": ("persistence.sqlite._state.write", "write_site"),
    "writer_mutex_until": ("persistence.sqlite._state.write", "writer_mutex_until"),
    "note_read_pool_exhausted": ("persistence.sqlite._state.readpool", "note_read_pool_exhausted"),
}

__all__ = sorted(_REPLACEMENTS)


def _transcript_view(include_inactive: "bool", include_compacted: "bool") -> "Any":
    from persistence.conversation.messages import TranscriptView

    if include_inactive:
        return TranscriptView.ALL
    return TranscriptView.DISPLAY if include_compacted else TranscriptView.ACTIVE


def _media_root() -> "str":
    from gateway.session_ingress_media import native_inputs_root

    return str(native_inputs_root())


def _scope_of(db_path: "Any") -> "Any":
    from persistence_assembly import profile_scope

    return profile_scope() if db_path is None else profile_scope(home=_Path(db_path).parent)


def _file_identity(scope: "Any") -> "Optional[tuple[int, int]]":
    """The device and inode of the file holding ``scope``'s store, or None when there is none."""
    from persistence_assembly import store_location

    location = store_location(scope)
    if location is None:
        return None
    try:
        status = _os.stat(location)
    except OSError:
        return None
    return status.st_dev, status.st_ino


def _held(method: "Any") -> "Any":
    """Run ``method`` with its ``SessionDB``'s store held open: a ``close()`` meanwhile releases it once the call
    returns."""

    @_functools.wraps(method)
    def call(self: "_SessionDB", *args: "Any", **kwargs: "Any") -> "Any":
        self._enter()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._leave()
    return call


# The store handles of writable stand-ins collected unclosed. A finalizer runs on whatever thread the collector does,
# which can hold a lock the release takes (the handle's, the shared-backend cache's, the registry's, a path's lifecycle
# lock, the read budget's, the WAL guard's), so it only puts the handle's stack here (SimpleQueue.put is safe in a
# finalizer) and the reaper thread closes it, holding no lock.
_collected_stacks: "_queue.SimpleQueue[_ExitStack]" = _queue.SimpleQueue()
_reaper: "Optional[_threading.Thread]" = None
_reaper_lock = _threading.Lock()
# Bound at import: a caller that later swaps threading.Thread for a synchronous stand-in (a test fixture) would
# otherwise run the reaper's endless loop on its own thread at its first writable open.
_ReaperThread = _threading.Thread


def _reap_collected_stacks() -> None:
    """The reaper: close each queued handle as it arrives, logging (never raising) a failed release."""
    while True:
        stack = _collected_stacks.get()
        try:
            stack.close()
        except Exception:  # health: allow BLE001 -- a collected handle has no caller to raise to
            _logger.debug("Releasing a collected hermes_state.SessionDB's store handle failed", exc_info=True)
        del stack  # never pin a released handle while waiting for the next one

    def _open_writer(self) -> None:
        """Writable open: preflight, zero-byte quarantine, connect + schema (one in-place repair of a
        malformed sqlite_master), generation stamp."""
        # Never materialize a deleted/archived named profile's home: a multiplexer or Desktop backend
        # still holding the profile's route would otherwise re-scaffold it on the next turn (#94590).
        mkdir_under_hermes_home(self.db_path.parent)
        try:
            # Only a positively observed SQLite header can bypass startup serialization.
            # Permission repair can reveal a damaged header after an indeterminate probe.
            if not has_valid_sqlite_header_preopen(self.db_path):
                # The directory must be writable before the lock file can be created in it; the
                # directory never vanishes mid-quarantine, so checking it unlocked is race-free.
                preflight_db_writability(self.db_path, db_label="state.db", directory_only=True)
                with quarantine_cross_process_lock(self.db_path) as lock_acquired:
                    if not lock_acquired:
                        logger.warning(
                            "startup quarantine lock for %s not acquired within 5s; proceeding",
                            self.db_path,
                        )
                    # Preflight under the lock: unlocked, it can list state.db just before a
                    # concurrent opener renames it aside, and os.access on the vanished path
                    # then reports a writable DB as "read-only for this user".
                    preflight_db_writability(self.db_path, db_label="state.db")
                    self._handle_quarantine_if_invalid(already_locked=lock_acquired)
                    self._connect_and_init_with_lock_patience()
            else:
                # Read-only file/sidecar preflight BEFORE the first connection: an actionable message
                # instead of an opaque "attempt to write a readonly database" from inside _init_schema.
                preflight_db_writability(self.db_path, db_label="state.db")
                self._handle_quarantine_if_invalid(already_locked=False)
                self._connect_and_init_with_lock_patience()
        except sqlite3.DatabaseError as exc:
            # A malformed schema fails on the very first statement (before _init_schema), so the
            # FTS-rebuild layer never sees it: repair sqlite_master in place (backup first), reopen once.
            if not is_malformed_schema_error(exc) or not _claim_repair_attempt(self.db_path):
                raise
            logger.error(
                "state.db schema is malformed (%s) — attempting automatic "
                "repair (a backup copy is made first).", exc,
            )
            self._close_connection_quietly(self._conn)
            if not repair_state_db_schema(self.db_path).get("repaired"):
                raise
            self._connect_and_init_with_lock_patience()
        # FTS optimization is OPT-IN (`hermes db optimize`); no background worker races session lifecycle.
        self._ensure_db_file_generation()
        if self._wal_active:
            # OFD copies of the two POSIX locks that keep a sibling's close from unlinking this WAL
            # generation: any in-process open()/close() of state.db or -shm cancels SQLite's own
            # (howtocorrupt §2.2); these survive it. Lifted in close().
            self._wal_lock_guard = _lockguard.hold(self.db_path)

def _ensure_reaper() -> None:
    """Start the reaper at the first writable open, or again whenever it isn't running."""
    global _reaper
    with _reaper_lock:
        if _reaper is None or not _reaper.is_alive():
            # health: allow HX012 -- the reaper releases handles on every profile's store and reads no profile state
            _reaper = _ReaperThread(target=_reap_collected_stacks, name="sessiondb-reaper", daemon=True)
            _reaper.start()

    def _connect_read_only(self, timeout: float) -> sqlite3.Connection:
        """``mode=ro`` tracked connection with Row factory. check_same_thread=False: pooled connections
        are borrowed by whichever thread reads next; exclusive ownership is enforced by pool checkout."""
        conn = _connect_tracked_db(
            read_only_db_uri(self.db_path), tracking_path=self.db_path, uri=True,
            check_same_thread=False, timeout=timeout, isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        from hermes_state_search import _register_search_functions
        _register_search_functions(conn)
        return conn

def _search_sort(sort: "Optional[str]", orders: "Any") -> "Any":
    """The old class's reading of ``sort``: ``newest`` or ``oldest`` in any case and padding, else relevance."""
    key = sort.strip().lower() if isinstance(sort, str) else ""
    return {order.value: order for order in orders}.get(key, orders.RELEVANCE)

    def _open_writer_conn(self) -> sqlite3.Connection:
        """Connect + WAL/pragma setup for a writer connection (no schema init). Short timeout:
        jittered application-level retry handles contention, not SQLite's busy handler;
        isolation_level=None: explicit BEGIN IMMEDIATE."""
        conn = _connect_tracked_db(
            str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None, factory=PublicationConnection,
        )
        try:
            conn.row_factory = sqlite3.Row
            from hermes_state_search import _register_search_functions
            _register_search_functions(conn)
            mode = apply_wal_with_fallback(conn, db_label="state.db")
            # "wal" is also the *assumed* mode when the on-disk probe was blocked by a concurrent opener
            # (#86515): the lock-free mode=ro read pool needs a confirmed WAL header, so confirm it here.
            # Unknown -> reads queue on the writer lock (slow but correct) instead of racing SQLITE_BUSY
            # on a file that may really be in rollback-journal mode.
            self._wal_active = mode == "wal" and _on_disk_journal_mode(conn) == "wal"
            # Existing WAL/SHM files may predate the main-file hardening;
            # normalize any sidecars that became visible during WAL setup.
            _secure_state_db_files(self.db_path)
            if self._wal_active and sys.platform == "darwin":
                # The periodic PASSIVE checkpoint runs off the writer lock. SQLite's
                # default auto-checkpoint can otherwise do the same fullfsync work in commit().
                conn.execute("PRAGMA wal_autocheckpoint=0")
            apply_database_pragmas(conn, db_label="state.db")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self._close_connection_quietly(conn)
            raise
        return conn

class _SessionDB:
    """``SessionDB`` over one store handle: ``open_persistence`` for a writable store (``db_path`` must name a
    ``state.db``), ``open_persistence_reader`` with ``read_only``. Each method forwards to the handle's repositories
    and returns the dict, list or scalar shapes the old class returned. ``close()`` releases the handle; a writable
    ``SessionDB`` used afterwards opens the same store again with a warning, a read-only one raises. A writable one
    collected unclosed has its handle released on the reaper thread."""

    def __init__(self, db_path: "Optional[Path]" = None, read_only: "bool" = False) -> None:
        from persistence_assembly import profile_scope

        self._target: Any
        self._stack: Optional[_ExitStack]
        self._store: Any
        if read_only:
            self._target = _Path(db_path) if db_path is not None else profile_scope().profile_dir
        else:
            if db_path is not None and _Path(db_path).name != "state.db":
                raise ValueError(
                    f"hermes_state.SessionDB opens a profile's state.db only, not {str(db_path)!r}; open the profile "
                    "folder's store with persistence_assembly.open_persistence(profile_scope(home=...))"
                )
            self._target = _scope_of(db_path)
        self.read_only = read_only
        self._lock = _threading.Lock()
        self._calls = 0
        self._closed = False
        self._reference: Optional[_weakref.finalize] = None
        self._stack, self._store = self._open()
        self._identity = None if read_only else _file_identity(self._target)

    def _open(self) -> "tuple[_ExitStack, Any]":
        from persistence_assembly import open_persistence, open_persistence_reader

        if not self.read_only:
            _ensure_reaper()  # before the acquire, so a reaper that cannot start leaves no handle open
        stack = _ExitStack()
        try:
            if self.read_only:
                store = stack.enter_context(open_persistence_reader(self._target))
                store.ready()
            else:
                store = stack.enter_context(open_persistence(self._target, eager=True))
        except BaseException:
            stack.close()
            raise
        if not self.read_only:
            # This handle's release: at close(), or by the reaper when this SessionDB is collected unclosed.
            self._reference = _weakref.finalize(self, _collected_stacks.put, stack)
            self._reference.atexit = False
        return stack, store

    def _enter(self) -> None:
        with self._lock:
            if self._closed:
                if self.read_only:
                    raise RuntimeError(f"hermes_state.SessionDB for {self._target} is closed (read-only)")
                if self._store is None:
                    self._refuse_reopen()
                    _logger.warning("hermes_state.SessionDB for %s was used after close(); reopening its store",
                                    self._target.profile_dir)
                    self._stack, self._store = self._open()
                self._closed = False
            self._calls += 1

    def _refuse_reopen(self) -> None:
        """Raise instead of opening the store again when the file at its path is not the one this ``SessionDB``
        opened (``StoreReplaced``) or the store is latched corrupt in this process (``StoreQuarantined``)."""
        from persistence import StoreQuarantined, StoreReplaced
        from persistence_assembly import storage_health

        if _file_identity(self._target) != self._identity:
            raise StoreReplaced(
                f"state.db in {self._target.profile_dir} was replaced after this hermes_state.SessionDB closed; "
                "refusing to open the new file through it")
        if storage_health(self._target).storage_state().value == "corrupt":
            raise StoreQuarantined(
                f"state.db in {self._target.profile_dir} is quarantined after structural corruption; refusing to "
                "open it again after close()")

    def _leave(self) -> None:
        with self._lock:
            self._calls -= 1
            released = self._detach_locked()
        if released is not None:
            released.close()

    def _detach_locked(self) -> "Optional[_ExitStack]":
        if not self._closed or self._calls or self._stack is None:
            return None
        released, self._stack, self._store = self._stack, None, None
        if self._reference is not None:
            self._reference.detach()
            self._reference = None
        return released

    def __getattr__(self, name: "str") -> "Any":
        raise AttributeError(
            f"hermes_state.SessionDB has no attribute {name!r}: it carries only the methods the plugin guide lists; "
            f"use the storage interface instead ({_MIGRATION_GUIDE})"
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            released = self._detach_locked()
        if released is not None:
            released.close()

    def __enter__(self) -> "_SessionDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- sessions --------------------------------------------------------------------------
    @_held
    def create_session(self, session_id: "str", source: "str", **kwargs: "Any") -> "str":
        from persistence.conversation.sessions import NewSession

        origin_json = kwargs.pop("origin_json", None)
        if origin_json is not None:
            kwargs["origin"] = _json.loads(origin_json)
        return self._store.sessions.create(NewSession(session_id=session_id, source=source, **kwargs))

    @_held
    def end_session(self, session_id: "str", end_reason: "str") -> None:
        self._store.sessions.end(session_id, end_reason)

    @_held
    def reopen_session(self, session_id: "str") -> None:
        self._store.sessions.reopen(session_id)

    @_held
    def get_session(self, session_id: "str") -> "Optional[dict[str, Any]]":
        record = self._store.sessions.get(session_id)
        return None if record is None else record.as_session_row()

    @_held
    def list_sessions_rich(
        self, source: "Optional[str]" = None, sources: "Optional[list[str]]" = None,
        exclude_sources: "Optional[list[str]]" = None, cwd_prefix: "Optional[str]" = None, limit: "int" = 20,
        offset: "int" = 0, include_children: "bool" = False, min_message_count: "int" = 0,
        project_compression_tips: "bool" = True, order_by_last_active: "bool" = False,
        include_archived: "bool" = False, archived_only: "bool" = False, id_query: "Optional[str]" = None,
        search_query: "Optional[str]" = None, compact_rows: "bool" = False, include_pinned: "bool" = False,
        session_key: "Optional[str]" = None, include_hidden: "bool" = False, include_subagents: "bool" = False,
        local_created_sources: "Optional[list[str]]" = None, workspace_key: "Optional[str]" = None,
    ) -> "list[dict[str, Any]]":
        from persistence.conversation.sessions import SessionOrder, SessionQuery, SessionWindow

        requested = [source] if source else sources
        query = SessionQuery(
            sources=tuple(requested) if requested else None, exclude_sources=tuple(exclude_sources or ()),
            cwd_prefix=cwd_prefix, include_children=include_children, min_message_count=min_message_count,
            project_compression_tips=project_compression_tips,
            order=SessionOrder.LAST_ACTIVE if order_by_last_active else SessionOrder.STARTED_AT,
            include_archived=include_archived, archived_only=archived_only, id_query=id_query,
            search_text=search_query, include_pinned=include_pinned, session_key=session_key,
            include_hidden=include_hidden, include_subagents=include_subagents,
            local_created_sources=tuple(local_created_sources) if local_created_sources else None,
            workspace_key=workspace_key)
        # The old class handed both to SQLite, where a negative LIMIT means every row and a negative OFFSET none.
        window = SessionWindow(limit=None if limit is not None and limit < 0 else limit, offset=max(offset, 0))
        entries = self._store.sessions.list_sessions(query, window, compact_rows=compact_rows)
        return [entry.as_list_row() for entry in entries]

    @_held
    def session_count(
        self, source: "Optional[str]" = None, sources: "Optional[list[str]]" = None,
        cwd_prefix: "Optional[str]" = None, min_message_count: "int" = 0, include_archived: "bool" = False,
        archived_only: "bool" = False, exclude_children: "bool" = False,
        exclude_sources: "Optional[list[str]]" = None, include_subagents: "bool" = False,
        local_created_sources: "Optional[list[str]]" = None,
    ) -> "int":
        from persistence.conversation.sessions import SessionCountQuery

        requested = [source] if source else sources
        return self._store.sessions.count_sessions(SessionCountQuery(
            sources=tuple(requested) if requested else None, exclude_sources=tuple(exclude_sources or ()),
            cwd_prefix=cwd_prefix, min_message_count=min_message_count, include_archived=include_archived,
            archived_only=archived_only, exclude_children=exclude_children, include_subagents=include_subagents,
            local_created_sources=tuple(local_created_sources) if local_created_sources else None))

    @_held
    def list_gateway_sessions(self, *, platform: "Optional[str]" = None,
                              active_only: "bool" = True) -> "list[dict[str, Any]]":
        return [{**entry.session.as_session_row(), "last_active": entry.last_active}
                for entry in self._store.sessions.list_gateway_sessions(platform=platform, active_only=active_only)]

    @_held
    def set_session_title(self, session_id: "str", title: "str") -> "bool":
        return self._store.sessions.set_title(session_id, title)

    @_held
    def resolve_session_by_title(self, title: "str") -> "Optional[str]":
        return self._store.sessions.resolve_by_title(title)

    @_held
    def resolve_session_id(self, session_id_or_prefix: "str") -> "Optional[str]":
        return self._store.sessions.resolve_id(session_id_or_prefix)

    @_held
    def get_next_title_in_lineage(self, base_title: "str") -> "str":
        return self._store.sessions.next_title_in_lineage(base_title)

    @_held
    def delete_session(
        self, session_id: "str", sessions_dir: "Optional[Path]" = None,
        expected_delete_ids: "Optional[list[str]]" = None,
        expected_display_messages: "Optional[dict[str, list[dict[str, Any]]]]" = None,
        exclude_active_write_guards: "bool" = False, include_compression_chain: "bool" = False,
    ) -> "bool":
        from session_retention import after_sessions_removed

        removed = self._store.sessions.delete(
            session_id, expected_ids=expected_delete_ids, expected_display=expected_display_messages,
            refuse_guarded=exclude_active_write_guards, include_compression_chain=include_compression_chain,
            media_root=_media_root())
        after_sessions_removed(self._store, self._store.scope.profile_dir, removed, reason="delete",
                               primary=session_id, sessions_dir=sessions_dir)
        return bool(removed)

    # -- messages --------------------------------------------------------------------------
    @_held
    def append_message(
        self, session_id: "str", role: "str", content: "Any" = None, tool_name: "Optional[str]" = None,
        tool_calls: "Any" = None, tool_call_id: "Optional[str]" = None, token_count: "Optional[int]" = None,
        finish_reason: "Optional[str]" = None, reasoning: "Optional[str]" = None,
        reasoning_content: "Optional[str]" = None, reasoning_details: "Any" = None,
        codex_reasoning_items: "Any" = None, codex_message_items: "Any" = None,
        platform_message_id: "Optional[str]" = None, observed: "bool" = False,
        effect_disposition: "Optional[str]" = None, _compressed_summary: "bool" = False, timestamp: "Any" = None,
        api_content: "Optional[str]" = None, display_kind: "Optional[str]" = None,
        display_metadata: "Optional[dict[str, Any]]" = None, compression_lock_holder: "Optional[str]" = None,
        turn_lease_holder: "Optional[str]" = None, turn_lease_ttl_seconds: "float" = 300.0,
        message_uid: "Optional[str]" = None,
    ) -> "int":
        """``compression_lock_holder`` is accepted and not passed on: the store's append takes no such argument."""
        from persistence.conversation.messages import NewMessage

        message = dict(
            role=role, content=content, tool_name=tool_name, tool_calls=tool_calls, tool_call_id=tool_call_id,
            token_count=token_count, finish_reason=finish_reason, reasoning=reasoning,
            reasoning_content=reasoning_content, reasoning_details=reasoning_details,
            codex_reasoning_items=codex_reasoning_items, codex_message_items=codex_message_items,
            platform_message_id=platform_message_id, observed=observed, effect_disposition=effect_disposition,
            _compressed_summary=_compressed_summary, timestamp=timestamp, api_content=api_content,
            display_kind=display_kind, display_metadata=display_metadata, message_uid=message_uid)
        return self._store.messages.append(
            session_id, NewMessage.from_live(message), turn_lease_holder=turn_lease_holder,
            turn_lease_ttl_seconds=turn_lease_ttl_seconds)

    @_held
    def get_messages(
        self, session_id: "str", include_inactive: "bool" = False, include_compacted: "bool" = False,
        limit: "Optional[int]" = None, offset: "int" = 0, latest: "bool" = False, after_id: "Optional[int]" = None,
        include_ancestors: "bool" = False,
    ) -> "list[dict[str, Any]]":
        from persistence.conversation.messages import TranscriptRead

        request = TranscriptRead(view=_transcript_view(include_inactive, include_compacted), limit=limit,
                                 offset=offset, latest=latest, after_id=after_id, include_lineage=include_ancestors)
        return [row.as_wire_row() for row in self._store.messages.read_transcript(session_id, request)]

    @_held
    def get_messages_as_conversation(
        self, session_id: "str", include_ancestors: "bool" = False, include_inactive: "bool" = False,
        repair_alternation: "bool" = False, include_row_ids: "bool" = False, include_compacted: "bool" = False,
    ) -> "list[dict[str, Any]]":
        from persistence.conversation.messages import ConversationRequest

        request = ConversationRequest(view=_transcript_view(include_inactive, include_compacted),
                                      include_lineage=include_ancestors, repair_alternation=repair_alternation,
                                      include_row_ids=include_row_ids)
        return [message.as_openai_message() for message in self._store.messages.read_conversation(session_id, request)]

    @_held
    def clear_messages(self, session_id: "str") -> None:
        self._store.messages.clear_transcript(session_id)

    @_held
    def search_messages(
        self, query: "str", source_filter: "Optional[list[str]]" = None,
        exclude_sources: "Optional[list[str]]" = None, role_filter: "Optional[list[str]]" = None, limit: "int" = 20,
        offset: "int" = 0, sort: "Optional[str]" = None, include_inactive: "bool" = False, fields: "Any" = None,
        after_ts: "Optional[int]" = None, before_ts: "Optional[int]" = None,
    ) -> "list[dict[str, Any]]":
        from persistence.conversation.search import SEARCH_RESULT_FIELDS, SearchSort

        hits = self._store.search.search_messages(
            query, source_filter=source_filter, exclude_sources=exclude_sources, role_filter=role_filter, limit=limit,
            offset=offset, sort=_search_sort(sort, SearchSort),
            include_inactive=include_inactive, fields=fields, after_ts=after_ts, before_ts=before_ts)
        names = SEARCH_RESULT_FIELDS if fields is None else tuple(f for f in SEARCH_RESULT_FIELDS if f in set(fields))
        return [_hit_dict(hit, names) for hit in hits]

    # -- export and prune ------------------------------------------------------------------
    @_held
    def export_session(self, session_id: "str", include_compacted: "bool" = False,
                       include_inactive: "bool" = False) -> "Optional[dict[str, Any]]":
        from hermes_cli.session_export import to_legacy_export_dict

        export = self._store.maintenance.export_session(session_id, include_compacted, include_inactive)
        return None if export is None else to_legacy_export_dict(export)

    @_held
    def export_all(self, source: "Optional[str]" = None, include_compacted: "bool" = False,
                   include_inactive: "bool" = False) -> "list[dict[str, Any]]":
        from hermes_cli.session_export import to_legacy_export_dict

        return [to_legacy_export_dict(export)
                for export in self._store.maintenance.export_all(source, include_compacted, include_inactive)]

    @_held
    def prune_sessions(
        self, older_than_days: "Optional[float]" = 90, source: "Optional[str]" = None,
        sessions_dir: "Optional[Path]" = None, exclude_active_write_guards: "bool" = False,
        deleted_ids: "Optional[list]" = None, **filters: "Any",
    ) -> "int":
        from persistence.housekeeping.maintenance import PruneCriteria
        from session_retention import after_sessions_removed

        outcome = self._store.maintenance.prune_sessions(
            older_than_days, source, criteria=PruneCriteria(**filters),
            exclude_active_write_guards=exclude_active_write_guards, media_root=_media_root())
        after_sessions_removed(self._store, self._store.scope.profile_dir, outcome.deleted_ids, reason="prune",
                               sessions_dir=sessions_dir)
        if deleted_ids is not None:
            deleted_ids.extend(outcome.deleted_ids)
        return outcome.count

    # -- state_meta ------------------------------------------------------------------------
    @_held
    def get_meta(self, key: "str") -> "Optional[str]":
        return self._store.state_meta.get(key)

    @_held
    def set_meta(self, key: "str", value: "str") -> None:
        self._store.state_meta.set(key, value)

    @_held
    def list_meta_prefix(self, prefix: "str") -> "list[tuple[str, str]]":
        if not prefix:
            return []
        return [(entry.key, entry.value) for entry in self._store.state_meta.list_prefix(prefix)]


def _hit_dict(hit: "Any", names: "tuple[str, ...]") -> "dict[str, Any]":
    """One search hit as the old result dict: ``id`` is the message id, ``context`` a list of role/content dicts."""
    row = {}
    for name in names:
        if name == "id":
            row[name] = hit.message_id
        elif name == "context":
            row[name] = [{"role": item.role, "content": item.content} for item in hit.context]
        else:
            row[name] = getattr(hit, name)
    return row


class _AsyncSessionDB:
    """Async door onto ``SessionDB``: every call runs through ``asyncio.to_thread``."""

    def __init__(self, db: "_SessionDB") -> None:
        self._db = db

    def __getattr__(self, name: "str") -> "Any":
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        async def _offloaded(*args: "Any", **kwargs: "Any") -> "Any":
            return await _asyncio.to_thread(attr, *args, **kwargs)
        return _offloaded


def _storage_state(db_path: "Any") -> "str":
    """``"corrupt"`` once this process latched structural damage on the store at ``db_path``, else ``"ok"``."""
    from persistence_assembly import storage_health

    return storage_health(_scope_of(db_path)).storage_state().value


def _storage_corrupt_reason(db_path: "Any") -> "Optional[str]":
    """``None`` while the store at ``db_path`` is not latched corrupt, else ``"corrupt"``: the interface keeps no
    latched error text."""
    state = _storage_state(db_path)
    return None if state == "ok" else state


def _divert_session_transcript_jsonl(session_id: "str", messages: "Any") -> "Optional[Path]":
    """Append pending messages to ``<HERMES_HOME>/sessions/<id>.jsonl``; the path, or None when there is nothing to
    write."""
    from hermes_constants import get_hermes_home
    from session_rescue import divert_session_transcript_jsonl

    return divert_session_transcript_jsonl(session_id, messages, sessions_dir=get_hermes_home() / "sessions")


_LOCAL = {
    "SessionDB": _SessionDB,
    "AsyncSessionDB": _AsyncSessionDB,
    "storage_state": _storage_state,
    "storage_corrupt_reason": _storage_corrupt_reason,
    "divert_session_transcript_jsonl": _divert_session_transcript_jsonl,
}


def _resolve(name: "str") -> "Any":
    if name == "DEFAULT_DB_PATH":
        from persistence_assembly import profile_scope, store_location

        return store_location(profile_scope())
    if name in _LOCAL:
        return _LOCAL[name]
    module, attr = _FORWARDED[name]
    return getattr(_importlib.import_module(module), attr)


def _first_warning() -> "bool":
    """True for the first caller in this process, which emits the stand-in's one deprecation warning."""
    global _warned
    first, _warned = not _warned, True
    return first


def _engine_session_db(engine: "Any", db_path: "Path", read_only: "bool") -> "_SessionDB":
    """The ``SessionDB`` Hermes hands a plugin context engine as ``session_db``. The stand-in's one warning goes to an
    engine with a session hook that cannot take ``persistence``, naming its class."""
    from agent.context_compressor_summary import accepts_keyword_argument

    db = _SessionDB(db_path, read_only=read_only)
    hooks = [getattr(engine, name, None) for name in ("bind_session_state", "on_session_start")]
    if not all(accepts_keyword_argument(hook, "persistence") for hook in hooks if callable(hook)) and _first_warning():
        _warnings.warn(
            f"context engine {type(engine).__module__}.{type(engine).__qualname__} gets session_db, a "
            f"hermes_state.SessionDB, which is deprecated and is removed in {_REMOVAL_RELEASE}; accept the "
            "persistence= keyword in its bind_session_state and on_session_start to read through the agent's store "
            "handle instead (https://hermes-agent.nousresearch.com/docs/developer-guide/context-engine-plugin"
            "#session-storage).", DeprecationWarning, stacklevel=3)
    return db


def __getattr__(name: "str") -> "Any":
    if name not in _REPLACEMENTS:
        raise AttributeError(
            f"module 'hermes_state' has no attribute {name!r}: the old session store was removed and this module "
            f"carries only the names the plugin guide lists ({_MIGRATION_GUIDE})"
        )
    value = _resolve(name)
    if _first_warning():
        _warnings.warn(
            f"hermes_state.{name} is deprecated and is removed in {_REMOVAL_RELEASE}; use {_REPLACEMENTS[name]} "
            f"instead ({_MIGRATION_GUIDE}).",
            DeprecationWarning, stacklevel=2)
    return value


def __dir__() -> "list[str]":
    return sorted({*globals(), *_REPLACEMENTS})
