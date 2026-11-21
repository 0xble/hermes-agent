"""Compatibility module for outside plugins that still import ``hermes_state``; removed in v0.27.0.

Session storage sits behind the storage interface (``persistence`` and ``persistence_assembly``). This module forwards
the names the plugin guide lists under "``hermes_state`` deprecated: use the storage interface" to that interface and
holds no SQL, database connection or state of its own. The first use of any of its names emits one
``DeprecationWarning`` per process naming that name's replacement and the removal release. Every other public name
raises ``AttributeError`` pointing at the guide, so the module's own helpers are underscore names.
"""

<<<<<<< HEAD
import asyncio
import atexit
import errno
import hashlib
import json
import logging
import os
import queue
import random
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager, suppress
from hermes_state_publication import PublicationConnection, state_lock, measure_write, raise_if_nonretryable_contention
from pathlib import Path
from hermes_dir_acl import make_user_dir
||||||| dff103e05878
import asyncio
import atexit
import errno
import hashlib
import json
import logging
import os
import queue
import random
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager, suppress
from pathlib import Path
from hermes_dir_acl import make_user_dir
=======
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
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

if _typing.TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Optional

<<<<<<< HEAD
from hermes_state_common import (
    TITLE_SOURCE_DERIVED as _TITLE_SOURCE_DERIVED, TITLE_SOURCE_LLM as _TITLE_SOURCE_LLM,
    TITLE_SOURCE_USER as _TITLE_SOURCE_USER,
    escape_like as _escape_like, _placeholders,
    stat_db_file_identity as _stat_db_file_identity,
)
from hermes_state_holders import read_only_db_uri
from hermes_state_pidns import holder_pid_checkable
from hermes_state_health import (
    STORAGE_CORRUPT, mark_storage_corrupt, note_storage_error, storage_corrupt_reason, storage_state,
)
from hermes_state_errors import (
    _DELETED_WAL_GENERATION_MSG, _DISK_IO_ERROR_MARKER, _STATE_DB_CORRUPT_MSG, _STATE_DB_GENERATION_KEY,
    _STATE_DB_REPLACED_MSG, DeletedWalGenerationError, SessionCompressionInProgressError, StateDbCorruptError,
    StateDbReplacedError, _is_no_more_rows, classify_persistence_error, is_malformed_db_error,
    is_malformed_schema_error, is_sqlite_lock_error,
)
from hermes_state_guard import (
    _STATE_DB_GUARD_BYPASS_ENV, _in_test_context, _is_production_state_db, _real_platform_state_root,
    _register_test_instance, _set_last_init_error, get_last_init_error,
)
from hermes_state_readpool import _READ_POOL_MAX, _proc_fd_targets, _read_budget_for, note_read_pool_exhausted
from hermes_state_sessions import SessionSessionsMixin
from hermes_state_fts import SessionFtsSetupMixin
from hermes_state_portability import SessionPortabilityMixin
from hermes_state_telegram import SessionTelegramTopicsMixin
from hermes_state_profile_repair import SessionProfileRepairMixin
from hermes_state_schema import SessionSchemaMixin
import hermes_state_holders as _state_holders
import hermes_state_lockguard as _lockguard
from hermes_state_lockowners import log_write_lock_holders
from hermes_loop_guard import LoopGuardedLock, check as check_loop_thread
from hermes_state_write import (
    WriterMutexTimeout as WriterMutexTimeout, maintain_after_commit, write_site as write_site,
    writer_mutex_until as writer_mutex_until,
)
from hermes_state_dbfile import (
    _chmod_pinned_inode, _connect_tracked_db, _fd_is_truly_unlinked, _is_operator_store_link,
    _prepare_connection_retirement,
    _read_sqlite_application_id, _stat_sqlite_sidecar_identity,
    _watched_sqlite_sidecar_paths, has_invalid_sqlite_header_preopen, has_valid_sqlite_header_preopen,
    is_zeroed_state_db, quarantine_cross_process_lock,
    quarantine_invalid_state_db,
    RetiredGenerationCaptureError, capture_retired_wal_generation, refuse_deleted_wal_generation,
)
from hermes_state_messages import SessionMessagesMixin
from hermes_state_prune_rewrite import SessionPruneRewriteMixin
from hermes_state_api_content import SessionApiContentMixin
from hermes_state_coverage import SessionCoverageMixin
from hermes_state_rewind import SessionRewindMixin
from hermes_state_wal import (
    _WAL_INCOMPAT_MARKERS, _on_disk_journal_mode, apply_database_pragmas, apply_wal_with_fallback,
)
from hermes_state_repair import _claim_repair_attempt, preflight_db_writability, repair_state_db_schema
from hermes_state_titles import SessionTitlesMixin
from hermes_state_prompt_refresh import SessionPromptRefreshMixin
from hermes_state_usage import SessionUsageMixin
from hermes_state_maintenance import SessionMaintenanceMixin
from hermes_state_gateway import SessionGatewayMixin
from hermes_state_compression import SessionCompressionMixin
from hermes_state_search import SessionSearchMixin
from hermes_state_stored_reads import SessionStoredReadsMixin
from hermes_state_transactions import settle_write_transaction
from hermes_state_write_stalls import observe_write, write_attempt

try:  # Hard dependency, but tolerate scaffold-phase imports before pip install.
    import psutil
except ImportError:  # pragma: no cover - stripped/scaffold installs only
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_MAX_SAFE_MESSAGES = 20_000  # resume/export guard default


def _configured_transcript_limit(key: str, fallback: int = _MAX_SAFE_MESSAGES) -> int:
    """``sessions.<key>`` from config.yaml (lazy import: circular at load), else *fallback*; 0 disables."""
    try:
        from hermes_cli.config import load_config_readonly
        value = (load_config_readonly().get("sessions") or {}).get(key)
        if value is None:
            return fallback
        limit = int(value)
        return limit if limit >= 0 else fallback
    except Exception:
        return fallback


def resolved_max_resume_messages() -> int:
    return _configured_transcript_limit("max_resume_messages")


def resolved_max_export_messages() -> int:
    return _configured_transcript_limit("max_export_messages")


class SessionResumeTooLargeError(ValueError):
    def __init__(
        self, message_count: int, limit: int = _MAX_SAFE_MESSAGES, scope: str = "across its lineage",
    ):
        self.message_count, self.limit = message_count, limit
        self.scope = scope
        super().__init__(
            f"This session is too long to reload safely ({message_count} messages; limit {limit}). "
            "Start a fresh chat and keep a copy with the dashboard Sessions page's Export action or "
            "`hermes sessions export --format md --session-id <id>` (neither is capped), or raise the limit "
            "with `hermes config set sessions.max_resume_messages 0`."
        )


class SessionExportTooLargeError(ValueError):
    def __init__(self, session_id: str, message_count: int, limit: int = _MAX_SAFE_MESSAGES):
        self.session_id, self.message_count, self.limit = session_id, message_count, limit
        # User-facing refusal shared by every in-memory JSON/JSONL export (CLI and console).
        super().__init__(
            f"Session '{session_id}' has more than {limit:,} exportable messages; the JSON/JSONL "
            "backup is built in memory and capped per session. Use the dashboard Sessions page's streaming "
            "Export action, or set sessions.max_export_messages: 0 in config.yaml to disable the guard."
        )


def _compression_lock_holder_process_is_dead(holder: str) -> bool:
    """True only when a ``pid=<n>`` lock holder's local PID is provably gone.
    Reclaim on kernel proof only: unstructured/same-process holders (another
    thread's live lease) and any probe doubt keep the lease until TTL expiry
    (PID reuse must never steal a live lease; a wrongly-kept one self-heals).
    Foreign/unstamped PID namespaces defer to TTL: see ``hermes_state_pidns``."""
    match = re.search(r"(?:^|:)pid=(\d+)(?::|$)", holder or "")
    pid = int(match.group(1)) if match else 0
    if pid <= 0 or pid == os.getpid():
        return False
    if not holder_pid_checkable(holder):
        return False  # foreign / unknown namespace: defer to TTL
    if psutil is not None:
        try:
            return not psutil.pid_exists(pid)  # recycled PIDs read as alive (conservative)
        except Exception:
            return False
    # psutil-less fallback is POSIX-only: on Windows os.kill(pid, 0) maps sig=0 to
    # CTRL_C_EVENT and can kill the target's console group.
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok — nt early-returns just above
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):  # PermissionError is an OSError: alive but foreign
        return False
    return False


# Billing buckets that aren't a routable provider identity: a session that persisted only
# one of these (never ran /model) falls back to the config default. Shared by
# session_gateway_runtime and tui_gateway.server so they cannot drift.
_BARE_BILLING_PROVIDERS = frozenset({"auto", "custom"})

T = TypeVar("T")

# Import-time snapshot lets _default_db_path() detect a re-pointed DEFAULT_DB_PATH
# (tests monkeypatch the constant directly).
DEFAULT_DB_PATH = _IMPORT_DEFAULT_DB_PATH = get_hermes_home() / "state.db"

# Back off from read-only opens after one fails: not per query, but short enough that
# transient fd pressure doesn't strand the read pool.
_READ_OPEN_RETRY_SECONDS = 60.0
# Transient SQLITE_IOERR retry budget for READ-ONLY opens (#100436): a WAL writer's checkpoint/
# reset/frame flush surfaces "disk I/O error" to a concurrent mode=ro reader for a millisecond-
# wide window — the ro connection cannot perform WAL recovery because recovery writes the -shm
# index, which mode=ro refuses. The writer closes the window on its own, so a few short retries
# make the open succeed instead of 500-ing the whole /api/sessions poll (or any other ro opener).
# Deliberately NOT for writable opens: a writer owns the transition, so an IOERR there is a real
# storage/fd problem. A persistent IOERR still exhausts the budget and propagates.
_READ_ONLY_IOERR_RETRY_ATTEMPTS, _READ_ONLY_IOERR_RETRY_BACKOFF_S = 3, 0.05
# SQLite busy handler budget for reads. Under DELETE (rollback-journal) mode a reader needs a SHARED
# lock, which every commit from another process blocks across its journal+db fsyncs; the writer
# connection's 1 s timeout exists for writes (they retry at application level) and starved readers
# into "database is locked" (dashboard 503s, `hermes sessions list` crashes) under a busy gateway.
_READ_BUSY_TIMEOUT_S = 5.0


def _default_db_path() -> Path:
    """Default state DB path at CALL time: a re-pointed ``DEFAULT_DB_PATH`` wins, else
    ``get_hermes_home()`` is resolved fresh (a runtime HERMES_HOME redirect works regardless of import)."""
    return DEFAULT_DB_PATH if DEFAULT_DB_PATH != _IMPORT_DEFAULT_DB_PATH else get_hermes_home() / "state.db"


# Live-DB guard knobs live HERE (not in hermes_state_guard): the hermetic conftest monkeypatches
# ``hermes_state._STATE_DB_GUARD_BYPASS`` (``@pytest.mark.live_system_guard_bypass`` escape hatch)
# and ``_EXTRA_DENY_ROOTS`` (the pre-sandbox root, so custom-HERMES_HOME deployments are covered).
_STATE_DB_GUARD_BYPASS = False
_STATE_DB_GUARD_EXTRA_DENY_ROOTS: tuple[Path, ...] = ()


def _ensure_test_isolation(db_path: Path) -> None:
    """Raise before any connection/mkdir/pragma/byte probe when a pytest-context process
    (env OR ancestry) resolves a production DB.

    Env alone is not enough: a child spawned with a rebuilt environment loses ``PYTEST_*`` and
    ``HERMES_HOME`` together, which is precisely the state in which it writes to production (#82770).
    """
    if _STATE_DB_GUARD_BYPASS or os.environ.get(_STATE_DB_GUARD_BYPASS_ENV) or not _in_test_context():
        return
    try:
        resolved = Path(db_path).expanduser().resolve()
    except Exception:
        return
    roots = [r for r in (_real_platform_state_root(),) if r is not None]
    for extra in _STATE_DB_GUARD_EXTRA_DENY_ROOTS:
        try:
            roots.append(Path(extra).expanduser().resolve())
        except Exception:
            continue
    for root in roots:
        if _is_production_state_db(resolved, root):
            raise RuntimeError(
                "live-system guard: test attempted to open production "
                f"state.db at {resolved} (under real Hermes root {root}). "
                "Tests must run against a temporary HERMES_HOME — pass an "
                "explicit tmp db_path or let the hermetic conftest redirect "
                "HERMES_HOME. If this test genuinely needs the live database, mark it with "
                "@pytest.mark.live_system_guard_bypass — or, for a spawned "
                f"child process, export {_STATE_DB_GUARD_BYPASS_ENV}=1 in "
                "its environment."
            )


def _secure_state_db_files(db_path: Path, *, create_main: bool = False) -> None:
    """Create/tighten a writable state database and its sidecars to 0600.

    SQLite otherwise creates ``state.db``, ``-wal``, and ``-shm`` according to
    the process umask (commonly 0644 under 0022). Read-only SessionDB
    attachments never call this helper and remain observational.

    Existing files are tightened without ever (re)opening them: opening the
    file and closing that descriptor would drop every POSIX ``fcntl`` lock the
    process holds on its inode — including the locks of an already-open SQLite
    connection to the same database. A lock-losing close in one process lets a
    sibling's connection take the shared-memory DMS exclusively at its own
    close, checkpoint, and unlink the sidecars while long-lived holders
    (gateway, desktop ``hermes serve``) keep using the deleted inodes.
    Symlinked state paths fail closed with ``ELOOP`` before the caller's
    ``sqlite3.connect()`` can follow them (#109857), except a ``state.db``
    link the operator made to a store of their own (#145075).
    """
    if os.name == "nt":
        return

    main_path = db_path
    if create_main:
        # O_EXCL: only a brand-new inode gets a descriptor. Opening an existing
        # file here and closing it would drop this process's POSIX locks on it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(main_path, flags, 0o600)
        except FileExistsError:
            pass
        except IsADirectoryError:
            # Not a database file at all; sqlite3.connect() raises the
            # canonical error for this, and a directory leaks no row data.
            return
        else:
            os.close(fd)

    for path in (
        main_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        # fchmod on an ordinary fd of a pre-existing file cannot be used here:
        # close(fd) would release this process's POSIX locks on that inode,
        # stripping the locks of any live SQLite connection to the same database.
        # _chmod_pinned_inode never takes a lock-bearing descriptor.
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            if path == main_path and _is_operator_store_link(path, st):
                # A store moved to other storage and linked back (#145075): SQLite follows the link as
                # before, and the target keeps the mode its owner gave it.
                continue
            # Fail closed exactly like the O_NOFOLLOW create above: silently
            # skipping the link lets the caller's sqlite3.connect() follow it
            # and operate on whatever it points at (#109857).
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(path))
        if not stat.S_ISREG(st.st_mode):
            continue
        try:
            _chmod_pinned_inode(path, 0o600)
        except FileNotFoundError:
            # SQLite's last holder can unlink a sidecar after the lstat.
            continue


# Openings of the background-review harness prompts (agent/background_review.py).
_REVIEW_HARNESS_PREFIXES = (
    "Review the conversation above and update the skill library",
    "Review the conversation above and consider saving to memory",
||||||| dff103e05878
from hermes_state_common import (
    TITLE_SOURCE_DERIVED as _TITLE_SOURCE_DERIVED, TITLE_SOURCE_LLM as _TITLE_SOURCE_LLM,
    TITLE_SOURCE_USER as _TITLE_SOURCE_USER,
    escape_like as _escape_like, _placeholders,
    stat_db_file_identity as _stat_db_file_identity,
)
from hermes_state_holders import read_only_db_uri
from hermes_state_pidns import holder_pid_checkable
from hermes_state_health import (
    STORAGE_CORRUPT, mark_storage_corrupt, note_storage_error, storage_corrupt_reason, storage_state,
)
from hermes_state_errors import (
    _DELETED_WAL_GENERATION_MSG, _DISK_IO_ERROR_MARKER, _STATE_DB_CORRUPT_MSG, _STATE_DB_GENERATION_KEY,
    _STATE_DB_REPLACED_MSG, DeletedWalGenerationError, SessionCompressionInProgressError, StateDbCorruptError,
    StateDbReplacedError, _is_no_more_rows, classify_persistence_error, is_malformed_db_error,
    is_malformed_schema_error, is_sqlite_lock_error,
)
from hermes_state_guard import (
    _STATE_DB_GUARD_BYPASS_ENV, _in_test_context, _is_production_state_db, _real_platform_state_root,
    _register_test_instance, _set_last_init_error, get_last_init_error,
)
from hermes_state_readpool import _READ_POOL_MAX, _proc_fd_targets, _read_budget_for, note_read_pool_exhausted
from hermes_state_sessions import SessionSessionsMixin
from hermes_state_fts import SessionFtsSetupMixin
from hermes_state_portability import SessionPortabilityMixin
from hermes_state_telegram import SessionTelegramTopicsMixin
from hermes_state_profile_repair import SessionProfileRepairMixin
from hermes_state_schema import SessionSchemaMixin
import hermes_state_holders as _state_holders
import hermes_state_lockguard as _lockguard
from hermes_state_lockowners import log_write_lock_holders
from hermes_state_write import WriterMutexTimeout, maintain_after_commit, write_site, writer_mutex_until
from hermes_state_dbfile import (
    _chmod_pinned_inode, _connect_tracked_db, _fd_is_truly_unlinked, _is_operator_store_link,
    _prepare_connection_retirement,
    _read_sqlite_application_id, _stat_sqlite_sidecar_identity,
    _watched_sqlite_sidecar_paths, has_invalid_sqlite_header_preopen, is_zeroed_state_db, quarantine_cross_process_lock,
    quarantine_invalid_state_db,
    RetiredGenerationCaptureError, capture_retired_wal_generation, refuse_deleted_wal_generation,
)
from hermes_state_messages import SessionMessagesMixin
from hermes_state_prune_rewrite import SessionPruneRewriteMixin
from hermes_state_api_content import SessionApiContentMixin
from hermes_state_coverage import SessionCoverageMixin
from hermes_state_rewind import SessionRewindMixin
from hermes_state_wal import (
    _WAL_INCOMPAT_MARKERS, _on_disk_journal_mode, apply_database_pragmas, apply_wal_with_fallback,
)
from hermes_state_repair import _claim_repair_attempt, preflight_db_writability, repair_state_db_schema
from hermes_state_titles import SessionTitlesMixin
from hermes_state_prompt_refresh import SessionPromptRefreshMixin
from hermes_state_usage import SessionUsageMixin
from hermes_state_maintenance import SessionMaintenanceMixin
from hermes_state_gateway import SessionGatewayMixin
from hermes_state_compression import SessionCompressionMixin
from hermes_state_search import SessionSearchMixin

try:  # Hard dependency, but tolerate scaffold-phase imports before pip install.
    import psutil
except ImportError:  # pragma: no cover - stripped/scaffold installs only
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_MAX_SAFE_MESSAGES = 20_000  # resume/export guard default


def _configured_transcript_limit(key: str, fallback: int = _MAX_SAFE_MESSAGES) -> int:
    """``sessions.<key>`` from config.yaml (lazy import: circular at load), else *fallback*; 0 disables."""
    try:
        from hermes_cli.config import load_config_readonly
        value = (load_config_readonly().get("sessions") or {}).get(key)
        if value is None:
            return fallback
        limit = int(value)
        return limit if limit >= 0 else fallback
    except Exception:
        return fallback


def resolved_max_resume_messages() -> int:
    return _configured_transcript_limit("max_resume_messages")


def resolved_max_export_messages() -> int:
    return _configured_transcript_limit("max_export_messages")


class SessionResumeTooLargeError(ValueError):
    def __init__(
        self, message_count: int, limit: int = _MAX_SAFE_MESSAGES, scope: str = "across its lineage",
    ):
        self.message_count, self.limit = message_count, limit
        self.scope = scope
        super().__init__(
            f"This session is too long to reload safely ({message_count} messages; limit {limit}). "
            "Start a fresh chat and keep a copy with the dashboard Sessions page's Export action or "
            "`hermes sessions export --format md --session-id <id>` (neither is capped), or raise the limit "
            "with `hermes config set sessions.max_resume_messages 0`."
        )


class SessionExportTooLargeError(ValueError):
    def __init__(self, session_id: str, message_count: int, limit: int = _MAX_SAFE_MESSAGES):
        self.session_id, self.message_count, self.limit = session_id, message_count, limit
        # User-facing refusal shared by every in-memory JSON/JSONL export (CLI and console).
        super().__init__(
            f"Session '{session_id}' has more than {limit:,} exportable messages; the JSON/JSONL "
            "backup is built in memory and capped per session. Use the dashboard Sessions page's streaming "
            "Export action, or set sessions.max_export_messages: 0 in config.yaml to disable the guard."
        )


def _compression_lock_holder_process_is_dead(holder: str) -> bool:
    """True only when a ``pid=<n>`` lock holder's local PID is provably gone.
    Reclaim on kernel proof only: unstructured/same-process holders (another
    thread's live lease) and any probe doubt keep the lease until TTL expiry
    (PID reuse must never steal a live lease; a wrongly-kept one self-heals).
    Foreign/unstamped PID namespaces defer to TTL: see ``hermes_state_pidns``."""
    match = re.search(r"(?:^|:)pid=(\d+)(?::|$)", holder or "")
    pid = int(match.group(1)) if match else 0
    if pid <= 0 or pid == os.getpid():
        return False
    if not holder_pid_checkable(holder):
        return False  # foreign / unknown namespace: defer to TTL
    if psutil is not None:
        try:
            return not psutil.pid_exists(pid)  # recycled PIDs read as alive (conservative)
        except Exception:
            return False
    # psutil-less fallback is POSIX-only: on Windows os.kill(pid, 0) maps sig=0 to
    # CTRL_C_EVENT and can kill the target's console group.
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok — nt early-returns just above
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):  # PermissionError is an OSError: alive but foreign
        return False
    return False


# Billing buckets that aren't a routable provider identity: a session that persisted only
# one of these (never ran /model) falls back to the config default. Shared by
# session_gateway_runtime and tui_gateway.server so they cannot drift.
_BARE_BILLING_PROVIDERS = frozenset({"auto", "custom"})

T = TypeVar("T")

# Import-time snapshot lets _default_db_path() detect a re-pointed DEFAULT_DB_PATH
# (tests monkeypatch the constant directly).
DEFAULT_DB_PATH = _IMPORT_DEFAULT_DB_PATH = get_hermes_home() / "state.db"

# Back off from read-only opens after one fails: not per query, but short enough that
# transient fd pressure doesn't strand the read pool.
_READ_OPEN_RETRY_SECONDS = 60.0
# Transient SQLITE_IOERR retry budget for READ-ONLY opens (#100436): a WAL writer's checkpoint/
# reset/frame flush surfaces "disk I/O error" to a concurrent mode=ro reader for a millisecond-
# wide window — the ro connection cannot perform WAL recovery because recovery writes the -shm
# index, which mode=ro refuses. The writer closes the window on its own, so a few short retries
# make the open succeed instead of 500-ing the whole /api/sessions poll (or any other ro opener).
# Deliberately NOT for writable opens: a writer owns the transition, so an IOERR there is a real
# storage/fd problem. A persistent IOERR still exhausts the budget and propagates.
_READ_ONLY_IOERR_RETRY_ATTEMPTS, _READ_ONLY_IOERR_RETRY_BACKOFF_S = 3, 0.05
# SQLite busy handler budget for reads. Under DELETE (rollback-journal) mode a reader needs a SHARED
# lock, which every commit from another process blocks across its journal+db fsyncs; the writer
# connection's 1 s timeout exists for writes (they retry at application level) and starved readers
# into "database is locked" (dashboard 503s, `hermes sessions list` crashes) under a busy gateway.
_READ_BUSY_TIMEOUT_S = 5.0


def _default_db_path() -> Path:
    """Default state DB path at CALL time: a re-pointed ``DEFAULT_DB_PATH`` wins, else
    ``get_hermes_home()`` is resolved fresh (a runtime HERMES_HOME redirect works regardless of import)."""
    return DEFAULT_DB_PATH if DEFAULT_DB_PATH != _IMPORT_DEFAULT_DB_PATH else get_hermes_home() / "state.db"


# Live-DB guard knobs live HERE (not in hermes_state_guard): the hermetic conftest monkeypatches
# ``hermes_state._STATE_DB_GUARD_BYPASS`` (``@pytest.mark.live_system_guard_bypass`` escape hatch)
# and ``_EXTRA_DENY_ROOTS`` (the pre-sandbox root, so custom-HERMES_HOME deployments are covered).
_STATE_DB_GUARD_BYPASS = False
_STATE_DB_GUARD_EXTRA_DENY_ROOTS: tuple[Path, ...] = ()


def _ensure_test_isolation(db_path: Path) -> None:
    """Raise before any connection/mkdir/pragma/byte probe when a pytest-context process
    (env OR ancestry) resolves a production DB.

    Env alone is not enough: a child spawned with a rebuilt environment loses ``PYTEST_*`` and
    ``HERMES_HOME`` together, which is precisely the state in which it writes to production (#82770).
    """
    if _STATE_DB_GUARD_BYPASS or os.environ.get(_STATE_DB_GUARD_BYPASS_ENV) or not _in_test_context():
        return
    try:
        resolved = Path(db_path).expanduser().resolve()
    except Exception:
        return
    roots = [r for r in (_real_platform_state_root(),) if r is not None]
    for extra in _STATE_DB_GUARD_EXTRA_DENY_ROOTS:
        try:
            roots.append(Path(extra).expanduser().resolve())
        except Exception:
            continue
    for root in roots:
        if _is_production_state_db(resolved, root):
            raise RuntimeError(
                "live-system guard: test attempted to open production "
                f"state.db at {resolved} (under real Hermes root {root}). "
                "Tests must run against a temporary HERMES_HOME — pass an "
                "explicit tmp db_path or let the hermetic conftest redirect "
                "HERMES_HOME. If this test genuinely needs the live database, mark it with "
                "@pytest.mark.live_system_guard_bypass — or, for a spawned "
                f"child process, export {_STATE_DB_GUARD_BYPASS_ENV}=1 in "
                "its environment."
            )


def _secure_state_db_files(db_path: Path, *, create_main: bool = False) -> None:
    """Create/tighten a writable state database and its sidecars to 0600.

    SQLite otherwise creates ``state.db``, ``-wal``, and ``-shm`` according to
    the process umask (commonly 0644 under 0022). Read-only SessionDB
    attachments never call this helper and remain observational.

    Existing files are tightened without ever (re)opening them: opening the
    file and closing that descriptor would drop every POSIX ``fcntl`` lock the
    process holds on its inode — including the locks of an already-open SQLite
    connection to the same database. A lock-losing close in one process lets a
    sibling's connection take the shared-memory DMS exclusively at its own
    close, checkpoint, and unlink the sidecars while long-lived holders
    (gateway, desktop ``hermes serve``) keep using the deleted inodes.
    Symlinked state paths fail closed with ``ELOOP`` before the caller's
    ``sqlite3.connect()`` can follow them (#109857), except a ``state.db``
    link the operator made to a store of their own (#145075).
    """
    if os.name == "nt":
        return

    main_path = db_path
    if create_main:
        # O_EXCL: only a brand-new inode gets a descriptor. Opening an existing
        # file here and closing it would drop this process's POSIX locks on it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(main_path, flags, 0o600)
        except FileExistsError:
            pass
        except IsADirectoryError:
            # Not a database file at all; sqlite3.connect() raises the
            # canonical error for this, and a directory leaks no row data.
            return
        else:
            os.close(fd)

    for path in (
        main_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        # fchmod on an ordinary fd of a pre-existing file cannot be used here:
        # close(fd) would release this process's POSIX locks on that inode,
        # stripping the locks of any live SQLite connection to the same database.
        # _chmod_pinned_inode never takes a lock-bearing descriptor.
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            if path == main_path and _is_operator_store_link(path, st):
                # A store moved to other storage and linked back (#145075): SQLite follows the link as
                # before, and the target keeps the mode its owner gave it.
                continue
            # Fail closed exactly like the O_NOFOLLOW create above: silently
            # skipping the link lets the caller's sqlite3.connect() follow it
            # and operate on whatever it points at (#109857).
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(path))
        if not stat.S_ISREG(st.st_mode):
            continue
        _chmod_pinned_inode(path, 0o600)


# Openings of the background-review harness prompts (agent/background_review.py).
_REVIEW_HARNESS_PREFIXES = (
    "Review the conversation above and update the skill library",
    "Review the conversation above and consider saving to memory",
=======
_REMOVAL_RELEASE = "v0.27.0"
_MIGRATION_GUIDE = (
    "https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#hermes_state-deprecated-use-the-storage-interface"
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
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

<<<<<<< HEAD

# ── Process-wide shared SessionDB registry (#90837) ── lives in hermes_state_registry.py (acquire /
# release / close_all / release_or_close). Long-lived in-process callers (gateway, tui_gateway, cron,
# in-process tools) share ONE writer connection per resolved path via hermes_state_registry.acquire(); CLI
# one-shots, recovery flows, and read-only cross-profile opens use SessionDB() directly with their own close().


@contextmanager
def _storage_write_in_progress():
    """Mark this thread as inside a SessionDB write (queued on the writer lock, retrying a busy
    database, or committing). The turn-liveness watchdog does not count that time as a stalled
    turn: under write contention a turn can queue behind dozens of writers for minutes."""
    thread = threading.current_thread()
    depth = getattr(thread, "_hermes_storage_write_depth", 0)
    thread._hermes_storage_write_depth = depth + 1
    try:
        yield
    finally:
        thread._hermes_storage_write_depth = depth
        thread._hermes_storage_write_ended_at = time.time()


def storage_write_activity(thread_id: Optional[int]) -> tuple[bool, Optional[float]]:
    """``(in_progress, last_ended_at)`` of SessionDB writes on thread *thread_id*."""
    thread = threading._active.get(thread_id) if thread_id is not None else None
    if thread is None:
        return False, None
    return (getattr(thread, "_hermes_storage_write_depth", 0) > 0,
            getattr(thread, "_hermes_storage_write_ended_at", None))


class SessionDB(
    SessionSessionsMixin, SessionFtsSetupMixin, SessionSearchMixin, SessionSchemaMixin,
    SessionPortabilityMixin, SessionTelegramTopicsMixin, SessionCompressionMixin,
    SessionGatewayMixin, SessionMaintenanceMixin, SessionUsageMixin, SessionTitlesMixin,
    SessionMessagesMixin, SessionPruneRewriteMixin, SessionApiContentMixin, SessionCoverageMixin, SessionRewindMixin, SessionProfileRepairMixin,
    SessionPromptRefreshMixin, SessionStoredReadsMixin,
):
    """SQLite-backed session storage with FTS5 search; many reader threads, one writer (WAL)."""

    # Only these state-owned producers join automatic stale-open reconciliation; messaging/UI
    # sources have their own lifecycle owners; unknown sources fail closed.
    # See #60609.  `recovered` = placeholders `hermes sessions recover` synthesizes for
    # orphaned messages (no live owner, never stamped ended_at); without it they are immortal.
    _AUTO_PRUNE_STALE_OPEN_SOURCES: tuple[str, ...] = (
        "cli", "cron", "kanban", "acp", "api_server", "subagent", "tool", "recovered",
    )

    # ── Write-contention tuning ──
    # SQLite's deterministic busy handler convoys under many hermes processes: keep its
    # timeout short (1s) and retry with random jitter. Patience is TIME-based (a sibling
    # legitimately holds the lock for seconds: checkpoint at close, VACUUM, recovery, FTS
    # optimize); attempt-counted budgets destroyed turns on a healthy store. Transcript
    # writes (failure aborts the turn) get the long budget; observation-only activity
    # writes sit on the response-critical path and get a sub-second one.
    _WRITE_PATIENCE_S, _TRANSCRIPT_WRITE_PATIENCE_S, _ACTIVITY_WRITE_PATIENCE_S = 20.0, 60.0, 0.5
    # A live compression lock gets a short wait (compression publishes in seconds), but the lease
    # is a correctness boundary: a writer still locked out afterwards is refused.
    # Observation-only activity heartbeat/label writes (#76354 review S1): these run on (or adjacent to) the
    # response-critical path and must never wait out the full routine patience under contention. Sub-second
    # budget; a skipped write is retried naturally at the next heartbeat window.
    # A live compression lock gets its own, much shorter budget than the write lock. Compression publishes
    # in a couple of seconds, so a brief wait saves the overwhelming majority of concurrent turns (#75083).
    # It deliberately stays short: the lease is a correctness boundary, not just a busy signal (see
    # test_compression_lease_blocks_non_owner_but_allows_owner_flush), so a writer that is still locked out
    # after this budget must still be refused rather than allowed to land a stale turn in a session whose
    # compression is genuinely long-running or wedged.
    _COMPRESSION_BUSY_WAIT_S = 5.0
    _WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S = 0.020, 0.150  # fast jitter for the first _SLOW_AFTER_S
    _WRITE_RETRY_SLOW_AFTER_S = 2.0
    _WRITE_RETRY_SLOW_MIN_S, _WRITE_RETRY_SLOW_MAX_S = 0.250, 1.000
    # Warn with callback and pid when one transaction holds the WAL write lock unusually long.
    _WRITE_LOCK_WARN_S = 3.0
    # PASSIVE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50
    # Bounded FTS ``'merge'`` (ms of lock each) instead of ``'optimize'`` (9-18s per index on a 10GB
    # DB, longer than a writer's patience); up to _COMMANDS_PER_PASS per index, stopping on no-progress.
    _FTS_MERGE_EVERY_N_WRITES, _FTS_MERGE_MAX_PAGES_PER_INDEX, _FTS_MERGE_COMMANDS_PER_PASS = 1000, 500, 4
    # Imports cap lower than exports: an import holds one BEGIN IMMEDIATE.
    _IMPORT_MAX_SESSIONS, _IMPORT_MAX_MESSAGES_PER_SESSION, _IMPORT_MAX_TOTAL_MESSAGES = 500, 10_000, 50_000
    _IMPORT_MAX_SESSION_BYTES, _IMPORT_MAX_TOTAL_BYTES = 5 * 1024 * 1024, 25 * 1024 * 1024
    # Accounting workers retire when idle so a bound-method target can't keep an abandoned SessionDB alive.
    _TOKEN_WRITER_IDLE_SECONDS = 30.0

    @staticmethod
    def _store_system_prompt(conn, system_prompt: Optional[str]) -> Optional[str]:
        if system_prompt is None:
            return None
        prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO system_prompts (hash, prompt) VALUES (?, ?)",
            (prompt_hash, system_prompt),
        )
        return prompt_hash

    @staticmethod
    def _delete_unreferenced_system_prompts(conn, limit: Optional[int] = None) -> int:
        if limit is None:
            cursor = conn.execute(
                "DELETE FROM system_prompts WHERE NOT EXISTS ("
                "SELECT 1 FROM sessions WHERE sessions.system_prompt_hash = system_prompts.hash) AND NOT EXISTS ("
                "SELECT 1 FROM sessions WHERE sessions.tool_names = system_prompts.hash)"
            )
            return max(cursor.rowcount, 0)
        rows = conn.execute(
            "SELECT hash FROM system_prompts WHERE NOT EXISTS ("
            "SELECT 1 FROM sessions WHERE sessions.system_prompt_hash = system_prompts.hash) AND NOT EXISTS ("
            "SELECT 1 FROM sessions WHERE sessions.tool_names = system_prompts.hash) LIMIT ?",
            (int(limit),),
        ).fetchall()
        if not rows:
            return 0
        hashes = [row[0] for row in rows]
        placeholders = ",".join("?" for _ in hashes)
        conn.execute(f"DELETE FROM system_prompts WHERE hash IN ({placeholders})", hashes)
        return len(hashes)

    @staticmethod
    def _session_row_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for column in ("system_prompt", "tool_names"):
            if f"_{column}_resolved" in data:
                resolved = data.pop(f"_{column}_resolved")
                if column in data:
                    data[column] = resolved
        # Lift /new vs /branch markers out of model_config so list payloads
        # (which strip that heavy field) can still tell a reset sibling from
        # a genuine fork. Keep this a local helper: tests sometimes replace
        # the SessionDB name with a factory lambda.
        if not (data.get("_reset_from") and data.get("_branched_from")):
            raw = data.get("model_config")
            cfg = None
            if isinstance(raw, str) and raw:
                try:
                    cfg = json.loads(raw)
                except (TypeError, ValueError):
                    cfg = None
            elif isinstance(raw, dict):
                cfg = raw
            if isinstance(cfg, dict):
                if not data.get("_reset_from"):
                    value = cfg.get("_reset_from")
                    if isinstance(value, str) and value.strip():
                        data["_reset_from"] = value.strip()
                if not data.get("_branched_from"):
                    value = cfg.get("_branched_from")
                    if isinstance(value, str) and value.strip():
                        data["_branched_from"] = value.strip()
        return data

    @staticmethod
    def _close_connection_quietly(conn: Optional[sqlite3.Connection]) -> None:
        """Close a partially initialized connection without masking its error."""
        if conn is None:
            return
||||||| dff103e05878

# ── Process-wide shared SessionDB registry (#90837) ── lives in hermes_state_registry.py (acquire /
# release / close_all / release_or_close). Long-lived in-process callers (gateway, tui_gateway, cron,
# in-process tools) share ONE writer connection per resolved path via hermes_state_registry.acquire(); CLI
# one-shots, recovery flows, and read-only cross-profile opens use SessionDB() directly with their own close().


class SessionDB(
    SessionSessionsMixin, SessionFtsSetupMixin, SessionSearchMixin, SessionSchemaMixin,
    SessionPortabilityMixin, SessionTelegramTopicsMixin, SessionCompressionMixin,
    SessionGatewayMixin, SessionMaintenanceMixin, SessionUsageMixin, SessionTitlesMixin,
    SessionMessagesMixin, SessionPruneRewriteMixin, SessionApiContentMixin, SessionCoverageMixin, SessionRewindMixin, SessionProfileRepairMixin,
    SessionPromptRefreshMixin,
):
    """SQLite-backed session storage with FTS5 search; many reader threads, one writer (WAL)."""

    # Only these state-owned producers join automatic stale-open reconciliation; messaging/UI
    # sources have their own lifecycle owners; unknown sources fail closed.
    # See #60609.  `recovered` = placeholders `hermes sessions recover` synthesizes for
    # orphaned messages (no live owner, never stamped ended_at); without it they are immortal.
    _AUTO_PRUNE_STALE_OPEN_SOURCES: tuple[str, ...] = (
        "cli", "cron", "kanban", "acp", "api_server", "subagent", "tool", "recovered",
    )

    # ── Write-contention tuning ──
    # SQLite's deterministic busy handler convoys under many hermes processes: keep its
    # timeout short (1s) and retry with random jitter. Patience is TIME-based (a sibling
    # legitimately holds the lock for seconds: checkpoint at close, VACUUM, recovery, FTS
    # optimize); attempt-counted budgets destroyed turns on a healthy store. Transcript
    # writes (failure aborts the turn) get the long budget; observation-only activity
    # writes sit on the response-critical path and get a sub-second one.
    _WRITE_PATIENCE_S, _TRANSCRIPT_WRITE_PATIENCE_S, _ACTIVITY_WRITE_PATIENCE_S = 20.0, 60.0, 0.5
    # A live compression lock gets a short wait (compression publishes in seconds), but the lease
    # is a correctness boundary: a writer still locked out afterwards is refused.
    # Observation-only activity heartbeat/label writes (#76354 review S1): these run on (or adjacent to) the
    # response-critical path and must never wait out the full routine patience under contention. Sub-second
    # budget; a skipped write is retried naturally at the next heartbeat window.
    # A live compression lock gets its own, much shorter budget than the write lock. Compression publishes
    # in a couple of seconds, so a brief wait saves the overwhelming majority of concurrent turns (#75083).
    # It deliberately stays short: the lease is a correctness boundary, not just a busy signal (see
    # test_compression_lease_blocks_non_owner_but_allows_owner_flush), so a writer that is still locked out
    # after this budget must still be refused rather than allowed to land a stale turn in a session whose
    # compression is genuinely long-running or wedged.
    _COMPRESSION_BUSY_WAIT_S = 5.0
    _WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S = 0.020, 0.150  # fast jitter for the first _SLOW_AFTER_S
    _WRITE_RETRY_SLOW_AFTER_S = 2.0
    _WRITE_RETRY_SLOW_MIN_S, _WRITE_RETRY_SLOW_MAX_S = 0.250, 1.000
    # PASSIVE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50
    # Bounded FTS ``'merge'`` (ms of lock each) instead of ``'optimize'`` (9-18s per index on a 10GB
    # DB, longer than a writer's patience); up to _COMMANDS_PER_PASS per index, stopping on no-progress.
    _FTS_MERGE_EVERY_N_WRITES, _FTS_MERGE_MAX_PAGES_PER_INDEX, _FTS_MERGE_COMMANDS_PER_PASS = 1000, 500, 4
    # Imports cap lower than exports: an import holds one BEGIN IMMEDIATE.
    _IMPORT_MAX_SESSIONS, _IMPORT_MAX_MESSAGES_PER_SESSION, _IMPORT_MAX_TOTAL_MESSAGES = 500, 10_000, 50_000
    _IMPORT_MAX_SESSION_BYTES, _IMPORT_MAX_TOTAL_BYTES = 5 * 1024 * 1024, 25 * 1024 * 1024
    # Accounting workers retire when idle so a bound-method target can't keep an abandoned SessionDB alive.
    _TOKEN_WRITER_IDLE_SECONDS = 30.0

    @staticmethod
    def _store_system_prompt(conn, system_prompt: Optional[str]) -> Optional[str]:
        if system_prompt is None:
            return None
        prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO system_prompts (hash, prompt) VALUES (?, ?)",
            (prompt_hash, system_prompt),
        )
        return prompt_hash

    @staticmethod
    def _delete_unreferenced_system_prompts(conn) -> None:
        conn.execute(
            "DELETE FROM system_prompts WHERE NOT EXISTS ("
            "SELECT 1 FROM sessions WHERE sessions.system_prompt_hash = system_prompts.hash) AND NOT EXISTS ("
            "SELECT 1 FROM sessions WHERE sessions.tool_names = system_prompts.hash)"
        )

    @staticmethod
    def _session_row_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for column in ("system_prompt", "tool_names"):
            if f"_{column}_resolved" in data:
                resolved = data.pop(f"_{column}_resolved")
                if column in data:
                    data[column] = resolved
        # Lift /new vs /branch markers out of model_config so list payloads
        # (which strip that heavy field) can still tell a reset sibling from
        # a genuine fork. Keep this a local helper: tests sometimes replace
        # the SessionDB name with a factory lambda.
        if not (data.get("_reset_from") and data.get("_branched_from")):
            raw = data.get("model_config")
            cfg = None
            if isinstance(raw, str) and raw:
                try:
                    cfg = json.loads(raw)
                except (TypeError, ValueError):
                    cfg = None
            elif isinstance(raw, dict):
                cfg = raw
            if isinstance(cfg, dict):
                if not data.get("_reset_from"):
                    value = cfg.get("_reset_from")
                    if isinstance(value, str) and value.strip():
                        data["_reset_from"] = value.strip()
                if not data.get("_branched_from"):
                    value = cfg.get("_branched_from")
                    if isinstance(value, str) and value.strip():
                        data["_branched_from"] = value.strip()
        return data

    @staticmethod
    def _close_connection_quietly(conn: Optional[sqlite3.Connection]) -> None:
        """Close a partially initialized connection without masking its error."""
        if conn is None:
            return
=======
    @_functools.wraps(method)
    def call(self: "_SessionDB", *args: "Any", **kwargs: "Any") -> "Any":
        self._enter()
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        try:
<<<<<<< HEAD
            conn.close()
        except Exception:
            logger.debug("Could not close a SessionDB connection", exc_info=True)

    def _close_conn_logged(self, conn, label: str) -> None:
        """Close *conn*; a failing close leaks a tracked fd: logged at WARNING, never swallowed."""
        try:
            conn.close()
        except Exception as exc:
            logger.warning("%s close failed for %s: %s", label, self.db_path, exc)

    def __init__(self, db_path: Path | None = None, read_only: bool = False):
        check_loop_thread("SessionDB open")
        self.db_path = db_path or _default_db_path()
        _ensure_test_isolation(self.db_path)  # before any connection/pragma/mkdir
        self.read_only = read_only
        # Keep only the opening call site, never a frame (which pins caller locals).
        self._creation_site = "unknown"
        caller = None
        try:
            caller = sys._getframe(1)
            self._creation_site = (
                f"{caller.f_globals.get('__name__', '?')}.{caller.f_code.co_name}:{caller.f_lineno}"
            )
        except Exception:
            pass  # Diagnostic metadata must not prevent opening the database.
||||||| dff103e05878
            conn.close()
        except Exception:
            logger.debug("Could not close a SessionDB connection", exc_info=True)

    def _close_conn_logged(self, conn, label: str) -> None:
        """Close *conn*; a failing close leaks a tracked fd: logged at WARNING, never swallowed."""
        try:
            conn.close()
        except Exception as exc:
            logger.warning("%s close failed for %s: %s", label, self.db_path, exc)

    def __init__(self, db_path: Path | None = None, read_only: bool = False):
        self.db_path = db_path or _default_db_path()
        _ensure_test_isolation(self.db_path)  # before any connection/pragma/mkdir
        self.read_only = read_only
        # Keep only the opening call site, never a frame (which pins caller locals).
        self._creation_site = "unknown"
        caller = None
        try:
            caller = sys._getframe(1)
            self._creation_site = (
                f"{caller.f_globals.get('__name__', '?')}.{caller.f_code.co_name}:{caller.f_lineno}"
            )
        except Exception:
            pass  # Diagnostic metadata must not prevent opening the database.
=======
            return method(self, *args, **kwargs)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        finally:
<<<<<<< HEAD
            del caller
        self._lock = LoopGuardedLock("SessionDB writer lock")
        # Read-path split (WAL only): reads borrow from a BOUNDED read-only pool so they
        # never queue behind writer flushes on self._lock (see _read_ctx); unbounded
        # per-thread connections pinned fds for the process lifetime and hit EMFILE.
        self._read_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=_READ_POOL_MAX)
        # Permits bound PEAK descriptors (the pool bounds only the idle set), shared per
        # DATABASE PATH; ordinary reads degrade to the writer lock, searches queue separately.
        # One permit per live read connection, held from before the open in _get_read_conn() until after the
        # close in _close_read_conn(). See _READ_POOL_MAX. Acquired non-blocking on purpose: a reader that
        # cannot get a permit must degrade (ordinary reads) or use bounded search admission — blocking here converts fd
        # exhaustion into a stall, which is the same outage with a different stack trace. Permits are shared
        # per DATABASE PATH, not per instance: the descriptors they ration belong to the file, and one
        # process holds several SessionDB objects on the same state.db (#98573). See _PathReadBudget.
        self._read_budget = _read_budget_for(self.db_path)
        self._read_permits = self._read_budget.permits
        self._read_conns_lock = threading.Lock()
        # Set when close() begins; an in-flight reader then closes its own connection
        # instead of re-populating a pool nobody will drain again.
        self._read_conns_closed = False
        # Read-open failure backoff is a TIMESTAMP, not a sticky bool: the likeliest trigger
        # is transient EMFILE, and a permanent flag would demote every reader forever.
        self._read_open_failed_at = 0.0
        self._wal_active, self._write_count = False, 0
        # File identity of the opened state.db, compared on every write so an out-of-band
        # replace cannot limp through in-place surgery (inode: mv/new-file; application_id: cp).
        self._db_file_identity: Optional[tuple] = None
        self._db_file_application_id: int = 0
        self._db_sidecar_identity: dict[str, tuple] = {}
        self._db_replaced = self._db_wal_generation_lost = False
        # Durable capture of a lost WAL generation (see _capture_retired_generation): once per handle.
        self._retired_generation_capture: Optional[Path] = None
        self._retired_capture_lock = threading.Lock()
        self._retire_connection: Optional[Callable[[Any], None]] = None
        self._connection_pinned = False  # one unmatched C reference taken at most once per handle
        self._wal_lock_guard: dict = {}  # hermes_state_lockguard.hold() record, see _open_writer
        # Periodic checkpoints run on their own connection, outside self._lock (see _try_wal_checkpoint).
        # Lock order: _checkpoint_lock before self._lock.
        self._checkpoint_lock = threading.Lock()
        self._checkpoint_conn: Optional[sqlite3.Connection] = None
        self._checkpoint_conn_writer: Any = None  # the writer connection it was opened beside
        self._db_corrupt, self._db_corrupt_reason = False, ""  # sticky quarantine (StateDbCorruptError)
        self._fts_usermerge_floor_applied = False  # one-shot usermerge-floor write guard
        self._fts_enabled = self._fts_stale = self._trigram_available = False
        self._fts_unavailable_warned = False
        self._conn = None
        # Async token accounting; distinct from self._lock so enqueue/flush never contends with writes.
        self._token_queue: deque = deque()
        self._token_queue_cond = threading.Condition(threading.Lock())
        self._token_writer_thread: Optional[threading.Thread] = None
        self._token_writer_stop = self._token_writer_busy = False
        self._token_atexit_hook: Optional[Callable[[], None]] = None
        # Opened via hermes_state_registry.acquire(): close() releases a refcount instead.
        # Set True when this instance is opened via hermes_state_registry.acquire(). Makes close() a no-op so the
        # registry (not individual callers) controls the connection lifecycle (#90837).
        self._shared_registry_owned = False
        initialization_complete = False
||||||| dff103e05878
            del caller
        self._lock = threading.Lock()
        # Read-path split (WAL only): reads borrow from a BOUNDED read-only pool so they
        # never queue behind writer flushes on self._lock (see _read_ctx); unbounded
        # per-thread connections pinned fds for the process lifetime and hit EMFILE.
        self._read_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=_READ_POOL_MAX)
        # Permits bound PEAK descriptors (the pool bounds only the idle set), shared per
        # DATABASE PATH; acquired non-blocking so a permitless reader degrades to the writer lock.
        # One permit per live read connection, held from before the open in _get_read_conn() until after the
        # close in _close_read_conn(). See _READ_POOL_MAX. Acquired non-blocking on purpose: a reader that
        # cannot get a permit must degrade to the writer lock, not queue here — blocking would convert fd
        # exhaustion into a stall, which is the same outage with a different stack trace. Permits are shared
        # per DATABASE PATH, not per instance: the descriptors they ration belong to the file, and one
        # process holds several SessionDB objects on the same state.db (#98573). See _PathReadBudget.
        self._read_budget = _read_budget_for(self.db_path)
        self._read_permits = self._read_budget.permits
        self._read_conns_lock = threading.Lock()
        # Set when close() begins; an in-flight reader then closes its own connection
        # instead of re-populating a pool nobody will drain again.
        self._read_conns_closed = False
        # Read-open failure backoff is a TIMESTAMP, not a sticky bool: the likeliest trigger
        # is transient EMFILE, and a permanent flag would demote every reader forever.
        self._read_open_failed_at = 0.0
        self._wal_active, self._write_count = False, 0
        # File identity of the opened state.db, compared on every write so an out-of-band
        # replace cannot limp through in-place surgery (inode: mv/new-file; application_id: cp).
        self._db_file_identity: Optional[tuple] = None
        self._db_file_application_id: int = 0
        self._db_sidecar_identity: dict[str, tuple] = {}
        self._db_replaced = self._db_wal_generation_lost = False
        # Durable capture of a lost WAL generation (see _capture_retired_generation): once per handle.
        self._retired_generation_capture: Optional[Path] = None
        self._retired_capture_lock = threading.Lock()
        self._retire_connection: Optional[Callable[[Any], None]] = None
        self._connection_pinned = False  # one unmatched C reference taken at most once per handle
        self._wal_lock_guard: dict = {}  # hermes_state_lockguard.hold() record, see _open_writer
        self._db_corrupt, self._db_corrupt_reason = False, ""  # sticky quarantine (StateDbCorruptError)
        self._fts_usermerge_floor_applied = False  # one-shot usermerge-floor write guard
        self._fts_enabled = self._fts_stale = self._trigram_available = False
        self._fts_unavailable_warned = False
        self._conn = None
        # Async token accounting; distinct from self._lock so enqueue/flush never contends with writes.
        self._token_queue: deque = deque()
        self._token_queue_cond = threading.Condition(threading.Lock())
        self._token_writer_thread: Optional[threading.Thread] = None
        self._token_writer_stop = self._token_writer_busy = False
        self._token_atexit_hook: Optional[Callable[[], None]] = None
        # Opened via hermes_state_registry.acquire(): close() releases a refcount instead.
        # Set True when this instance is opened via hermes_state_registry.acquire(). Makes close() a no-op so the
        # registry (not individual callers) controls the connection lifecycle (#90837).
        self._shared_registry_owned = False
        initialization_complete = False
=======
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
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        try:
            stack.close()
        except Exception:  # health: allow BLE001 -- a collected handle has no caller to raise to
            _logger.debug("Releasing a collected hermes_state.SessionDB's store handle failed", exc_info=True)
        del stack  # never pin a released handle while waiting for the next one

<<<<<<< HEAD
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
||||||| dff103e05878
    def _open_writer(self) -> None:
        """Writable open: preflight, zero-byte quarantine, connect + schema (one in-place repair of a
        malformed sqlite_master), generation stamp."""
        # Never materialize a deleted/archived named profile's home: a multiplexer or Desktop backend
        # still holding the profile's route would otherwise re-scaffold it on the next turn (#94590).
        mkdir_under_hermes_home(self.db_path.parent)
        try:
            # Serialize zero-byte check, quarantine, connect and schema commit so concurrent
            # openers don't race the absent-path -> schema-commit window.
            if not self.db_path.exists() or has_invalid_sqlite_header_preopen(self.db_path):
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
=======
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

def _ensure_reaper() -> None:
    """Start the reaper at the first writable open, or again whenever it isn't running."""
    global _reaper
    with _reaper_lock:
        if _reaper is None or not _reaper.is_alive():
            # health: allow HX012 -- the reaper releases handles on every profile's store and reads no profile state
            _reaper = _ReaperThread(target=_reap_collected_stacks, name="sessiondb-reaper", daemon=True)
            _reaper.start()

<<<<<<< HEAD
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
||||||| dff103e05878
    def _connect_read_only(self, timeout: float) -> sqlite3.Connection:
        """``mode=ro`` tracked connection with Row factory. check_same_thread=False: pooled connections
        are borrowed by whichever thread reads next; exclusive ownership is enforced by pool checkout."""
        conn = _connect_tracked_db(
            read_only_db_uri(self.db_path), tracking_path=self.db_path, uri=True,
            check_same_thread=False, timeout=timeout, isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        return conn
=======
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

def _search_sort(sort: "Optional[str]", orders: "Any") -> "Any":
    """The old class's reading of ``sort``: ``newest`` or ``oldest`` in any case and padding, else relevance."""
    key = sort.strip().lower() if isinstance(sort, str) else ""
    return {order.value: order for order in orders}.get(key, orders.RELEVANCE)

<<<<<<< HEAD
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
            apply_database_pragmas(conn, db_label="state.db")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self._close_connection_quietly(conn)
            raise
        return conn
||||||| dff103e05878
    def _open_writer_conn(self) -> sqlite3.Connection:
        """Connect + WAL/pragma setup for a writer connection (no schema init). Short timeout:
        jittered application-level retry handles contention, not SQLite's busy handler;
        isolation_level=None: explicit BEGIN IMMEDIATE."""
        conn = _connect_tracked_db(
            str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            mode = apply_wal_with_fallback(conn, db_label="state.db")
            # "wal" is also the *assumed* mode when the on-disk probe was blocked by a concurrent opener
            # (#86515): the lock-free mode=ro read pool needs a confirmed WAL header, so confirm it here.
            # Unknown -> reads queue on the writer lock (slow but correct) instead of racing SQLITE_BUSY
            # on a file that may really be in rollback-journal mode.
            self._wal_active = mode == "wal" and _on_disk_journal_mode(conn) == "wal"
            # Existing WAL/SHM files may predate the main-file hardening;
            # normalize any sidecars that became visible during WAL setup.
            _secure_state_db_files(self.db_path)
            apply_database_pragmas(conn, db_label="state.db")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self._close_connection_quietly(conn)
            raise
        return conn
=======
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

class _SessionDB:
    """``SessionDB`` over one store handle: ``open_persistence`` for a writable store (``db_path`` must name a
    ``state.db``), ``open_persistence_reader`` with ``read_only``. Each method forwards to the handle's repositories
    and returns the dict, list or scalar shapes the old class returned. ``close()`` releases the handle; a writable
    ``SessionDB`` used afterwards opens the same store again with a warning, a read-only one raises. A writable one
    collected unclosed has its handle released on the reaper thread."""

    def __init__(self, db_path: "Optional[Path]" = None, read_only: "bool" = False) -> None:
        from persistence_assembly import profile_scope

<<<<<<< HEAD
    # ── Read-path split ──

    def _get_read_conn(self, *, independent: bool = False) -> Optional[sqlite3.Connection]:
        """Open a fresh read-only connection, or None when unavailable (callers
        return it to self._read_pool). Ordinary reads retain their legacy non-WAL
        fallback. Independent searches always use mode=ro, even on read-only handles
        or DELETE journals, where SQLite contention surfaces explicitly instead.
        Autocommit sees commits so far (read-your-writes for flush-then-search)."""
        if not independent and (not self._wal_active or self.read_only):
            return None
        with self._read_conns_lock:
            failed_at = self._read_open_failed_at
            backing_off = failed_at and time.monotonic() - failed_at < _READ_OPEN_RETRY_SECONDS
            if independent and self._read_conns_closed:
                raise sqlite3.OperationalError("session database closed during search")
            if self._read_conns_closed or (backing_off and not independent):
                return None
        # Permit BEFORE the open: openers race for permits, not descriptors.
        if not self._read_budget.acquire(self):
            if independent:
                logger.debug("read pool at capacity (%d) for %s; search queued for a reader", _READ_POOL_MAX, self.db_path)
            else:
                note_read_pool_exhausted(self.db_path)
            return None
        conn = None  # bound before the try so the handlers can close a half-open one
        try:
            conn = self._connect_read_only(timeout=_READ_BUSY_TIMEOUT_S)
            apply_database_pragmas(conn, db_label="state.db")
        except BaseException as exc:
            # A half-open connection (open ok, pragmas failed) is a live tracked descriptor,
            # the leak shape this pool exists to fix; a stranded permit would shrink the read
            # path by one slot forever. (Not _close_read_conn: callers release their own permit.)
            if conn is not None:
                self._close_conn_logged(conn, "partially-opened read conn")
            self._read_budget.release()
            if independent or not isinstance(exc, sqlite3.Error):
                raise
            with self._read_conns_lock:
                self._read_open_failed_at = time.monotonic()
            logger.debug("read-only connection open failed for %s", self.db_path, exc_info=True)
            return None
        return conn

    def _evict_one_idle_read_conn(self) -> bool:
        """Close one idle pooled connection (a peer on the same file wants its permit); never a live one."""
        try:
            conn = self._read_pool.get_nowait()
        except queue.Empty:
            return False
        self._close_read_conn(conn)
        return True

    def _close_read_conn(self, conn) -> None:
        """Close a pooled read connection and release its permit even when the close fails (withholding
        it would narrow the read path forever). Over-releasing the BoundedSemaphore raises ValueError."""
        try:
            self._close_conn_logged(conn, "read-conn")
        finally:
            self._read_budget.release()

    def _checkout_read_conn(self, *, independent: bool = False) -> Optional[sqlite3.Connection]:
        """Borrow a read connection, opening on a miss; None when the read path is unavailable.
        A pool hit costs no permit (the connection already holds one)."""
        if not independent and (not self._wal_active or self.read_only):
            return None
        if independent and self._read_conns_closed:
            raise sqlite3.OperationalError("session database closed during search")
        try:
            return self._read_pool.get_nowait()
        except queue.Empty:
            return self._get_read_conn(independent=True) if independent else self._get_read_conn()

    def _return_read_conn(self, conn) -> None:
        with self._read_conns_lock:
            if not self._read_conns_closed:
                try:
                    self._read_pool.put_nowait(conn)
                    return
                except queue.Full:
                    pass
        self._close_read_conn(conn)

    @contextmanager
    def _read_ctx(self, *, reopen: bool = True, resident_only: bool = False, pooled_only: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a connection for read-only statements: a pooled read-only
        connection with NO lock under WAL; otherwise (non-WAL, open failure,
        ceiling reached) the writer connection under self._lock — deliberate
        degradation: slower beats EMFILE, which the supervisor cannot see. A search
        scope overrides that fallback for all of its reads, including hydration.
        ``reopen=False``: a read after ``close()`` raises instead of reopening the
        writer. Pinned snapshots and search operations retain their own connection.
        ``pooled_only`` yields None when no independent reader is available.
        """
        if resident_only:
            if not self._lock.acquire(blocking=False):
                raise RuntimeError("Resident session read unavailable")
            try:
                if self._conn is None:
                    raise RuntimeError("Resident session read unavailable")
                yield self._conn
            finally:
                self._lock.release()
            return
        from hermes_state_search_runtime import current_search
        operation = current_search(self)
        if operation is not None:
            yield operation.connection
            return
        pinned = self._pinned_read_conn()
        if pinned is not None:
            yield pinned
            return
        conn = self._checkout_read_conn()
        if conn is not None:
            try:
                yield conn
            finally:
                self._return_read_conn(conn)
            return
        if pooled_only:
            yield None
            return
        with state_lock(self._lock, db_path=self.db_path):
            if self._conn is None:  # close() raced a still-unwinding reader
                if not reopen:
                    raise sqlite3.ProgrammingError(f"SessionDB for {self.db_path} is closed")
                self._reopen_after_close_locked(context="read")
            conn = cast(sqlite3.Connection, self._conn)
            if self._wal_active or self.read_only:  # WAL: no SHARED-lock wait; ro: opened with the read budget
                yield conn
                return
            # DELETE-mode writer connection: wait out a sibling's commit like a pooled reader would.
            previous_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            conn.execute(f"PRAGMA busy_timeout={int(_READ_BUSY_TIMEOUT_S * 1000)}")
            try:
                yield conn
            finally:
                with suppress(sqlite3.Error):
                    conn.execute(f"PRAGMA busy_timeout={int(previous_ms)}")

    def _reopen_after_close_locked(self, context: str = "write") -> None:
        """Reopen the writer after ``close()`` raced a live caller (a teardown owner
        set ``_conn = None`` while a worker still had a transcript flush to land).
        Loud (WARNING) and bounded (only after an explicit close()). Caller holds
        ``self._lock``. No _init_schema: no DDL races with siblings during teardown."""
        if self.read_only:
            raise sqlite3.ProgrammingError(
                f"SessionDB for {self.db_path} was closed (read-only handle); "
                f"cannot serve a {context} after close()"
            )
        # A reopen resolves the PATH again: a replaced file would be written through stale WAL/shm
        # assumptions; a quarantined handle must never hand a fresh connection to a damaged file.
        if self._db_corrupt and not (self._db_replaced or self._db_file_was_replaced()):
            raise self._corrupt_error(
                f"state.db connection for {self.db_path} is quarantined after "
                f"structural corruption; refusing to reopen for a {context} "
                "after close(). "
            )
        self._halt_if_db_generation_changed()
        logger.warning(
            "state.db connection for %s was closed while a %s was still in "
            "flight — reopening (teardown/worker race, #94736)", self.db_path, context,
        )
        try:
            self._conn = self._open_writer_conn()
        except Exception as exc:
            raise sqlite3.OperationalError(
                f"state.db connection was closed while a {context} was still "
                f"in flight (a session-teardown path called close() before "
                f"this worker finished — #94736) and the automatic reopen failed: {exc}"
            ) from exc
        if self._wal_active:  # a reopened writer is a live generation holder like the first open
            self._wal_lock_guard = _lockguard.hold(self.db_path)

    def _execute_write(
        self, fn: Callable[[sqlite3.Connection], T], patience_s: Optional[float] = None,
        *, session_id: Optional[str] = None, input_rows: Optional[int] = None,
        session_ids: Any = (), skip_result: Any = None,
    ) -> T:
        """Run *fn(conn)* in a write transaction; see :meth:`_execute_write_marked`."""
        with observe_write(self.db_path, fn), _storage_write_in_progress(), measure_write():
            return self._execute_write_marked(
                fn, patience_s, session_ids=session_ids, skip_result=skip_result, session_id=session_id, input_rows=input_rows)

    def _execute_write_marked(
        self, fn: Callable[[sqlite3.Connection], T], patience_s: Optional[float] = None,
        *, session_id: Optional[str] = None, input_rows: Optional[int] = None,
        session_ids: Any = (), skip_result: Any = None,
    ) -> T:
        """Run *fn(conn)* inside BEGIN IMMEDIATE with jittered lock retry; commit
        is handled here (callers must not commit). Returns *fn*'s result.
        BEGIN IMMEDIATE takes the WAL write lock up front so contention surfaces
        immediately; on locked/busy the Python lock is released, a jitter slept,
        and the WHOLE callback retried — *fn* must stay idempotent under retry."""
        # Metrics and privacy share the same session identity. Transcript transactions
        # pass the singular form, while batches pass session_ids.
        session_ids = (session_ids, session_id)
        if patience_s is None:
            patience_s = self._WRITE_PATIENCE_S
        deadline = time.monotonic() + patience_s
        compression_deadline: Optional[float] = None  # set on the first compression-busy collision
        # One retry for SQLITE_IOERR raised by BEGIN IMMEDIATE itself (callback not run: nothing
        # replayed). Once fn has started, an IOERR leaves settlement unknown and must propagate.
        # The callback has not run at that point, so there is no durable effect to replay and the retry is
        # exactly-once safe (#99502's contract). Once the callback starts, an IOERR leaves the write's
        # settlement unknown and must propagate — this helper owns non-idempotent transcript/counter
        # mutations, not just idempotent UPSERTs.
        ioerr_begin_retried = False
        from hermes_state_incognito import private_write, write_admission
        while True:
            if private_write(session_ids):
                return cast(T, skip_result)
            self._raise_if_db_corrupt(storage=True)
            # NOTE: the AUTHORITATIVE replaced/generation probe runs INSIDE the lock below.
            # close() mutates _conn and _db_sidecar_identity under that same lock, ending the
            # WAL generation (SQLite unlinks the -wal/-shm sidecars on a clean close). A
            # lock-free probe that races close() can observe the mid-teardown state — sidecars
            # already unlinked while _db_sidecar_identity is not yet cleared — and misclassify
            # this process's OWN clean close as an externally deleted generation, raising a
            # sticky DeletedWalGenerationError that permanently refuses later writes (#105567).
            # The lock-free pre-probe below can therefore only CLEAR the healthy path: any doubt
            # (a mismatch, nothing recorded, an error) defers to the in-lock probe, which only
            # ever sees the stable post-close state. Its stat()/pread syscalls stall in the
            # kernel on a saturated disk, and under the lock that stalled every queued writer.
            generation_clean = self._db_generation_probe_clean()
            fn_started = False
            try:
                acquisition_started = time.monotonic()
                with write_attempt(), state_lock(self._lock, db_path=self.db_path, deadline=deadline), write_admission(session_ids) as admitted:
                    if not admitted:
                        return cast(T, skip_result)
                    if generation_clean:
                        self._raise_if_db_generation_flagged()
                    else:
                        self._raise_if_db_replaced()
                    if private_write(session_ids):
                        return cast(T, skip_result)
                    if self._conn is None:  # close() raced this writer
                        self._reopen_after_close_locked(context="write")
                    conn = cast(sqlite3.Connection, self._conn)  # reopen succeeded under the writer guard
                    conn.execute("BEGIN IMMEDIATE")
                    fn_started = True
                    result = settle_write_transaction(conn, fn, clock=time, logger=logger,
                        warn_s=self._WRITE_LOCK_WARN_S, acquire_s=time.monotonic() - acquisition_started,
                        session_id=session_id, input_rows=input_rows)
                # Success — periodic best-effort checkpoint + FTS merge.
                maintain_after_commit(self)
                return result
            except SessionCompressionInProgressError as exc:
                raise_if_nonretryable_contention(exc)
                # Transient (see _COMPRESSION_BUSY_WAIT_S): a steer landing mid-compression must not abort.
                # A live foreign compression lock is transient: the compressor publishes in a couple of
                # seconds. Without any wait, a steer that lands mid-compression aborts the user's turn as
                # session_persistence_failed and sends the operator hunting disk space that was never the
                # problem (#75083). The budget is _COMPRESSION_BUSY_WAIT_S, not the write-lock patience: the
                # lease is a correctness boundary, so a writer still locked out after a short wait must be
                # refused rather than left to land a stale turn once a long-running or wedged compression
                # finally lets go.
                if compression_deadline is None:
                    compression_deadline = min(time.monotonic() + self._COMPRESSION_BUSY_WAIT_S, deadline)
                if self._sleep_before_write_retry(
                    compression_deadline, self._COMPRESSION_BUSY_WAIT_S
                ):
                    continue
                raise
            except sqlite3.Error as exc:
                raise_if_nonretryable_contention(exc)
                # 'no more rows' is a transient engine error on contended WAL appends (some builds
                # raise it as InterfaceError, a sibling of DatabaseError): retry like locked/busy.
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(deadline, patience_s):
                    continue
                err_msg = str(exc).lower()
                if isinstance(exc, sqlite3.OperationalError):
                    if is_sqlite_lock_error(exc):
                        if self._sleep_before_write_retry(deadline, patience_s):
                            continue
                        # Say what actually happened, not disk/permission damage. The holder goes to
                        # the log, not the message: classify_persistence_error() buckets by phrase and
                        # a holder's argv (a worktree named fix-corrupt-db) would flip the bucket.
                        log_write_lock_holders(self.db_path, patience_s)
                        raise sqlite3.OperationalError(
                            f"database is locked (another Hermes process held the "
                            f"state.db write lock for over {patience_s:.0f}s — "
                            "likely a long maintenance operation such as VACUUM, "
                            "a large WAL checkpoint, or an older pre-update "
                            "process; the database itself is healthy)"
                        ) from exc
                    if (
                        _DISK_IO_ERROR_MARKER in err_msg and not fn_started and not ioerr_begin_retried
                        and self._sleep_before_write_retry(deadline, patience_s)
                    ):
                        # Retry on the SAME connection: close()+reopen would cancel this process's
                        # POSIX locks for every sibling (howtocorrupt §2.2).
                        ioerr_begin_retried = True
                        continue
                    raise  # non-lock error, callback already ran, or patience exhausted
                if isinstance(exc, sqlite3.DatabaseError):
                    # An out-of-band replace surfaces as this same corruption class; in-file repair
                    # on a NEW generation amplifies the damage.
                    if (
                        "not a database" in err_msg or is_malformed_db_error(exc)
                        or self._is_fts_write_corruption_error(exc)
                    ):
                        with self._lock:
                            self._raise_if_db_replaced()
                    # Corrupt FTS shadow tables fail every write via the sync triggers while canonical
                    # rows are intact: detach the derived indexes atomically and retry (never rebuild here).
                    if self._enter_fts_fail_open(exc, deadline=deadline, patience_s=patience_s):
                        continue
                    # What survives both checks is structural damage: quarantine.
                    if self._is_structural_corruption_error(exc):
                        self._halt_db_corrupt(exc)
                raise

    def _write_sql(
        self, sql: str, params: Any = (), *, many: bool = False, patience_s: Optional[float] = None,
        session_ids: Any = (),
    ) -> None:
        """Run one INSERT/UPDATE/DELETE through ``_execute_write``."""
        def _do(conn):
            (conn.executemany if many else conn.execute)(sql, params)
        self._execute_write(_do, patience_s=patience_s, session_ids=(session_ids, params,))

    def _write_rowcount(self, sql: str, params: Any = (), *, patience_s: Optional[float] = None,
                        session_ids: Any = ()) -> int:
        """Run one UPDATE/DELETE through ``_execute_write``; return rows changed
        (``SELECT changes()`` when the driver reports None / negative)."""
        def _do(conn):
            rowcount = conn.execute(sql, params).rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount
        return self._execute_write(_do, patience_s=patience_s, session_ids=(session_ids, params,), skip_result=0)

    def _read_one(self, sql: str, params: Any = ()) -> Optional[sqlite3.Row]:
        """``fetchone()`` of one read-only statement via ``_read_ctx``."""
        return self._read_retrying_ioerr(lambda conn: conn.execute(sql, params).fetchone())

    def _read_all(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        """``fetchall()`` of one read-only statement via ``_read_ctx``."""
        return self._read_retrying_ioerr(lambda conn: conn.execute(sql, params).fetchall())

    def _read_retrying_ioerr(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run an idempotent SELECT through ``_read_ctx``, retrying a transient SQLITE_IOERR.

        A warm ``mode=ro`` pooled reader can hit the same millisecond-wide WAL transition window as a
        read-only OPEN (#100436) when its statement executes or steps: a sibling process's checkpoint /
        WAL reset / frame flush surfaces ``disk I/O error`` because a read-only connection cannot rewrite
        the -shm index (#100871, WSL2 ext4-on-vhdx, multi-process). The window closes on its own, so
        the statement is replayed on the SAME connection within the read-only IOERR budget -- never
        closed and reopened (close() cancels this process's POSIX locks for every sibling connection),
        never quarantined (busy is not broken). A persistent IOERR exhausts the budget and propagates."""
        for attempt in range(_READ_ONLY_IOERR_RETRY_ATTEMPTS + 1):
            try:
                with self._read_ctx() as conn:
                    from hermes_state_search_runtime import search_stage
                    with search_stage(self, "sql_ms"):
                        return fn(conn)
            except sqlite3.OperationalError as exc:
                if attempt >= _READ_ONLY_IOERR_RETRY_ATTEMPTS or _DISK_IO_ERROR_MARKER not in str(exc).lower():
                    note_storage_error(self.db_path, exc)
                    raise
                time.sleep(_READ_ONLY_IOERR_RETRY_BACKOFF_S)
            except sqlite3.DatabaseError as exc:
                # A reader is often the only observer (the sidebar poll on a store nobody is
                # writing to): publish structural damage so the list is not read as empty.
                note_storage_error(self.db_path, exc)
                raise

    def _ensure_db_file_generation(self) -> None:
        """Mint a once-per-file generation stamp (state_meta + application_id). First opener wins (INSERT
        OR IGNORE); application_id is written only while 0 so racers converge. Skip the checkpoint only
        when the generation's application_id is already visible in the main file.

        See #45383.
        """
        if self.read_only or self._conn is None:
            return
        token = uuid.uuid4().hex
        try:
            with self._lock:
                # Read first: the stamp is minted once per file, and a no-op INSERT OR IGNORE
                # still takes the write lock — under a sibling's transaction it blocked for the
                # busy timeout and the except below then dropped the token entirely. First
                # opener still wins via INSERT OR IGNORE; racers converge on the re-read.
                row = self._conn.execute(
                    "SELECT value FROM state_meta WHERE key = ?", (_STATE_DB_GENERATION_KEY,),
                ).fetchone()
                generation_needs_checkpoint = not (row and row[0])
                if generation_needs_checkpoint:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO state_meta (key, value) VALUES (?, ?)",
                        (_STATE_DB_GENERATION_KEY, token),
                    )
                    row = self._conn.execute(
                        "SELECT value FROM state_meta WHERE key = ?",
                        (_STATE_DB_GENERATION_KEY,),
                    ).fetchone()
                if row and row[0]:
                    token = str(row[0])
                pragma_row = self._conn.execute("PRAGMA application_id").fetchone()
                current = int(pragma_row[0] or 0) if pragma_row else 0
                if current == 0:
                    current = (int(token[:8], 16) & 0x7FFFFFFF) or 1
                    self._conn.execute(f"PRAGMA application_id={current}")
                    generation_needs_checkpoint = True
                self._db_file_application_id = current
                if generation_needs_checkpoint or _read_sqlite_application_id(self.db_path) != current:
                    try:
                        # A reader can block the first PASSIVE checkpoint; WAL-visible
                        # values alone do not prove durability. The header is written
                        # after the row, so retry until it reaches the main DB. Use the
                        # lock-safe probe; a settled warm open still skips the write.
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except sqlite3.Error:
                        pass
        except sqlite3.Error as exc:
            logger.debug("state.db generation stamp skipped: %s", exc)

    def _record_db_file_identity(self) -> None:
        """Snapshot inode plus the on-disk generation header when present."""
        self._db_file_identity = _stat_db_file_identity(self.db_path)
        self._db_sidecar_identity = _stat_sqlite_sidecar_identity(self.db_path)
        disk_id = _read_sqlite_application_id(self.db_path)
        if disk_id:
            self._db_file_application_id = disk_id
        elif self._conn is not None and not self._db_file_application_id:
            try:
                pragma_row = self._read_one("PRAGMA application_id")
            except sqlite3.Error:
                pragma_row = None
            if pragma_row and pragma_row[0]:
                self._db_file_application_id = int(pragma_row[0])

    def _db_file_was_replaced(self) -> bool:
        """True when the path no longer names the file this instance opened."""
        recorded = self._db_file_identity
        if recorded is not None and _stat_db_file_identity(self.db_path) != recorded:
            return True
        recorded_app = int(self._db_file_application_id or 0)
        if not recorded_app:
            return False
        # Header 0 = WAL not yet checkpointed, not a replace; a real replacement is nonzero.
        disk_app = _read_sqlite_application_id(self.db_path)
        return bool(disk_app and disk_app != recorded_app)

    def _wal_generation_was_lost(self) -> bool:
        """True when the WAL/SHM generation this handle opened is gone. Recorded
        generation: pure stat (no /proc walk on healthy writes). Empty identity
        (WAL appeared after open, or cleared by a clean close()): probe
        /proc/self/fd for deleted sidecars and adopt the current ones once clean."""
        recorded = self._db_sidecar_identity or {}
        base = os.fspath(self.db_path)
        if recorded:
            return any(
                _stat_db_file_identity(Path(base + suffix)) != ident for suffix, ident in recorded.items()
            )
        if not self._wal_active:  # no sidecar generation to lose; keep /proc off the hot path
            return False
        if sys.platform.startswith("linux"):
            watched = _watched_sqlite_sidecar_paths(self.db_path)
            try:
                for target, fd_path in _proc_fd_targets(os.getpid()):
                    canonical = _state_holders.canonical_sqlite_path(target)
                    if (" (deleted)" in target and canonical in watched
                            and _fd_is_truly_unlinked(fd_path, watched[canonical])):
                        return True
            except OSError:
                return False
        # Probe clean (or unavailable): adopt the current sidecar generation.
        current_identity = _stat_sqlite_sidecar_identity(self.db_path)
        if current_identity:
            self._db_sidecar_identity = current_identity
        return False

    def _halt_if_db_generation_changed(self) -> None:
        """Stop writes (logging once) when the file was replaced or its WAL/SHM generation
        is gone: never run in-file repair on a new generation, never keep committing on a
        split WAL. Both flags are sticky."""
        # A reopen resolves the PATH again — if the file at that path is no longer the one this instance
        # originally opened (out-of-band restore/cp/mv), reconnecting would write into the new generation
        # through stale WAL/shm assumptions (#89332). Refuse instead.
        if self._db_replaced or self._db_file_was_replaced():
            self._db_replaced = True
            self._disable_close_time_checkpoint()
            logger.error(_STATE_DB_REPLACED_MSG)
            raise StateDbReplacedError(_STATE_DB_REPLACED_MSG)
        if self._db_wal_generation_lost or self._wal_generation_was_lost():
            self._db_wal_generation_lost = True
            self._disable_close_time_checkpoint()
            try:
                self._capture_retired_generation("halt")
            except RetiredGenerationCaptureError as exc:
                logger.error(
                    "Could not capture the retired WAL generation of %s at halt: %s. close() retries "
                    "the capture and refuses to settle without it.", self.db_path, exc,
||||||| dff103e05878
    # ── Read-path split ──

    def _get_read_conn(self) -> Optional[sqlite3.Connection]:
        """Open a fresh read-only connection, or None when unavailable (callers
        return it to self._read_pool). WAL only: WAL readers never block on the
        writer, so reads skip self._lock; under DELETE journal mode (NFS fallback)
        readers hit SQLITE_BUSY storms, so the legacy locked path stays. Autocommit
        reads see everything committed so far (read-your-writes for flush-then-search)."""
        if not self._wal_active or self.read_only:
            return None
        with self._read_conns_lock:
            failed_at = self._read_open_failed_at
            backing_off = failed_at and time.monotonic() - failed_at < _READ_OPEN_RETRY_SECONDS
            if self._read_conns_closed or backing_off:
                return None
        # Permit BEFORE the open: openers race for permits, not descriptors.
        if not self._read_budget.acquire(self):
            note_read_pool_exhausted(self.db_path)
            return None
        conn = None  # bound before the try so the handlers can close a half-open one
        try:
            conn = self._connect_read_only(timeout=_READ_BUSY_TIMEOUT_S)
            apply_database_pragmas(conn, db_label="state.db")
        except BaseException as exc:
            # A half-open connection (open ok, pragmas failed) is a live tracked descriptor,
            # the leak shape this pool exists to fix; a stranded permit would shrink the read
            # path by one slot forever. (Not _close_read_conn: callers release their own permit.)
            if conn is not None:
                self._close_conn_logged(conn, "partially-opened read conn")
            self._read_budget.release()
            if not isinstance(exc, sqlite3.Error):
                raise
            with self._read_conns_lock:
                self._read_open_failed_at = time.monotonic()
            logger.debug("read-only connection open failed for %s", self.db_path, exc_info=True)
            return None
        return conn

    def _evict_one_idle_read_conn(self) -> bool:
        """Close one idle pooled connection (a peer on the same file wants its permit); never a live one."""
        try:
            conn = self._read_pool.get_nowait()
        except queue.Empty:
            return False
        self._close_read_conn(conn)
        return True

    def _close_read_conn(self, conn) -> None:
        """Close a pooled read connection and release its permit even when the close fails (withholding
        it would narrow the read path forever). Over-releasing the BoundedSemaphore raises ValueError."""
        try:
            self._close_conn_logged(conn, "read-conn")
        finally:
            self._read_budget.release()

    def _checkout_read_conn(self) -> Optional[sqlite3.Connection]:
        """Borrow a read connection, opening on a miss; None when the read path is unavailable.
        A pool hit costs no permit (the connection already holds one)."""
        if not self._wal_active or self.read_only:
            return None
        try:
            return self._read_pool.get_nowait()
        except queue.Empty:
            return self._get_read_conn()

    @contextmanager
    def _read_ctx(self, *, reopen: bool = True, pooled_only: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a connection for read-only statements: a pooled read-only
        connection with NO lock under WAL; otherwise (non-WAL, open failure,
        ceiling reached) the writer connection under self._lock — deliberate
        degradation: slower beats EMFILE, which the supervisor cannot see.
        ``reopen=False``: a read after ``close()`` raises instead of reopening the
        writer (an optional reader must not hold a home that is being deleted). Inside this thread's
        ``transcript_read_snapshot``: its connection, so every read there sees one generation.
        ``pooled_only``: None instead of the writer connection when no pooled reader is available."""
        pinned = self._pinned_read_conn()
        if pinned is not None:
            yield pinned
            return
        conn = self._checkout_read_conn()
        if conn is not None:
            try:
                yield conn
            finally:
                returned = False
                with self._read_conns_lock:
                    if not self._read_conns_closed:
                        try:
                            self._read_pool.put_nowait(conn)
                            returned = True
                        except queue.Full:
                            pass
                if not returned:
                    # close() drained the pool (or queue.Full: unreachable while
                    # permits == maxsize, load-bearing if they drift): surplus.
                    self._close_read_conn(conn)
            return
        if pooled_only:
            yield None
            return
        with self._lock:
            if self._conn is None:  # close() raced a still-unwinding reader
                if not reopen:
                    raise sqlite3.ProgrammingError(f"SessionDB for {self.db_path} is closed")
                self._reopen_after_close_locked(context="read")
            conn = cast(sqlite3.Connection, self._conn)
            if self._wal_active or self.read_only:  # WAL: no SHARED-lock wait; ro: opened with the read budget
                yield conn
                return
            # DELETE-mode writer connection: wait out a sibling's commit like a pooled reader would.
            previous_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            conn.execute(f"PRAGMA busy_timeout={int(_READ_BUSY_TIMEOUT_S * 1000)}")
            try:
                yield conn
            finally:
                with suppress(sqlite3.Error):
                    conn.execute(f"PRAGMA busy_timeout={int(previous_ms)}")

    def _reopen_after_close_locked(self, context: str = "write") -> None:
        """Reopen the writer after ``close()`` raced a live caller (a teardown owner
        set ``_conn = None`` while a worker still had a transcript flush to land).
        Loud (WARNING) and bounded (only after an explicit close()). Caller holds
        ``self._lock``. No _init_schema: no DDL races with siblings during teardown."""
        if self.read_only:
            raise sqlite3.ProgrammingError(
                f"SessionDB for {self.db_path} was closed (read-only handle); "
                f"cannot serve a {context} after close()"
            )
        # A reopen resolves the PATH again: a replaced file would be written through stale WAL/shm
        # assumptions; a quarantined handle must never hand a fresh connection to a damaged file.
        if self._db_corrupt and not (self._db_replaced or self._db_file_was_replaced()):
            raise self._corrupt_error(
                f"state.db connection for {self.db_path} is quarantined after "
                f"structural corruption; refusing to reopen for a {context} "
                "after close(). "
            )
        self._halt_if_db_generation_changed()
        logger.warning(
            "state.db connection for %s was closed while a %s was still in "
            "flight — reopening (teardown/worker race, #94736)", self.db_path, context,
        )
        try:
            self._conn = self._open_writer_conn()
        except Exception as exc:
            raise sqlite3.OperationalError(
                f"state.db connection was closed while a {context} was still "
                f"in flight (a session-teardown path called close() before "
                f"this worker finished — #94736) and the automatic reopen failed: {exc}"
            ) from exc
        if self._wal_active:  # a reopened writer is a live generation holder like the first open
            self._wal_lock_guard = _lockguard.hold(self.db_path)

    def _execute_write(
        self, fn: Callable[[sqlite3.Connection], T], patience_s: Optional[float] = None,
    ) -> T:
        """Run *fn(conn)* inside BEGIN IMMEDIATE with jittered lock retry; commit
        is handled here (callers must not commit). Returns *fn*'s result.
        BEGIN IMMEDIATE takes the WAL write lock up front so contention surfaces
        immediately; on locked/busy the Python lock is released, a jitter slept,
        and the WHOLE callback retried — *fn* must stay idempotent under retry."""
        if patience_s is None:
            patience_s = self._WRITE_PATIENCE_S
        deadline = time.monotonic() + patience_s
        compression_deadline: Optional[float] = None  # set on the first compression-busy collision
        # One retry for SQLITE_IOERR raised by BEGIN IMMEDIATE itself (callback not run: nothing
        # replayed). Once fn has started, an IOERR leaves settlement unknown and must propagate.
        # The callback has not run at that point, so there is no durable effect to replay and the retry is
        # exactly-once safe (#99502's contract). Once the callback starts, an IOERR leaves the write's
        # settlement unknown and must propagate — this helper owns non-idempotent transcript/counter
        # mutations, not just idempotent UPSERTs.
        ioerr_begin_retried = False
        while True:
            self._raise_if_db_corrupt(storage=True)
            # NOTE: the replaced/generation live probe runs INSIDE the lock below,
            # not here. close() mutates _conn and _db_sidecar_identity under that
            # same lock, ending the WAL generation (SQLite unlinks the -wal/-shm
            # sidecars on a clean close). A lock-free probe that races close() can
            # observe the mid-teardown state — sidecars already unlinked while
            # _db_sidecar_identity is not yet cleared — and misclassify this
            # process's OWN clean close as an externally deleted generation,
            # raising a sticky DeletedWalGenerationError that permanently refuses
            # later writes (#105567). Inside the lock the probe only ever sees the
            # stable post-close state (identity cleared → adopt / reopen path).
            fn_started = False
            try:
                with writer_mutex_until(self._lock, deadline, db=self, site=write_site(fn)):
                    self._raise_if_db_replaced()
                    if self._conn is None:  # close() raced this writer
                        self._reopen_after_close_locked(context="write")
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        fn_started = True
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint + FTS merge.
                maintain_after_commit(self)
                return result
            except WriterMutexTimeout:
                # No transaction began; preserve local contention attribution and never replay it.
                raise
            except SessionCompressionInProgressError:
                # Transient (see _COMPRESSION_BUSY_WAIT_S): a steer landing mid-compression must not abort.
                # A live foreign compression lock is transient: the compressor publishes in a couple of
                # seconds. Without any wait, a steer that lands mid-compression aborts the user's turn as
                # session_persistence_failed and sends the operator hunting disk space that was never the
                # problem (#75083). The budget is _COMPRESSION_BUSY_WAIT_S, not the write-lock patience: the
                # lease is a correctness boundary, so a writer still locked out after a short wait must be
                # refused rather than left to land a stale turn once a long-running or wedged compression
                # finally lets go.
                if compression_deadline is None:
                    compression_deadline = min(time.monotonic() + self._COMPRESSION_BUSY_WAIT_S, deadline)
                if self._sleep_before_write_retry(
                    compression_deadline, self._COMPRESSION_BUSY_WAIT_S
                ):
                    continue
                raise
            except sqlite3.Error as exc:
                # 'no more rows' is a transient engine error on contended WAL appends (some builds
                # raise it as InterfaceError, a sibling of DatabaseError): retry like locked/busy.
                if _is_no_more_rows(exc) and self._sleep_before_write_retry(deadline, patience_s):
                    continue
                err_msg = str(exc).lower()
                if isinstance(exc, sqlite3.OperationalError):
                    if is_sqlite_lock_error(exc):
                        if self._sleep_before_write_retry(deadline, patience_s):
                            continue
                        # Say what actually happened, not disk/permission damage. The holder goes to
                        # the log, not the message: classify_persistence_error() buckets by phrase and
                        # a holder's argv (a worktree named fix-corrupt-db) would flip the bucket.
                        log_write_lock_holders(self.db_path, patience_s)
                        raise sqlite3.OperationalError(
                            f"database is locked (another Hermes process held the "
                            f"state.db write lock for over {patience_s:.0f}s — "
                            "likely a long maintenance operation such as VACUUM, "
                            "a large WAL checkpoint, or an older pre-update "
                            "process; the database itself is healthy)"
                        ) from exc
                    if (
                        _DISK_IO_ERROR_MARKER in err_msg and not fn_started and not ioerr_begin_retried
                        and self._sleep_before_write_retry(deadline, patience_s)
                    ):
                        # Retry on the SAME connection: close()+reopen would cancel this process's
                        # POSIX locks for every sibling (howtocorrupt §2.2).
                        ioerr_begin_retried = True
                        continue
                    raise  # non-lock error, callback already ran, or patience exhausted
                if isinstance(exc, sqlite3.DatabaseError):
                    # An out-of-band replace surfaces as this same corruption class; in-file repair
                    # on a NEW generation amplifies the damage.
                    if (
                        "not a database" in err_msg or is_malformed_db_error(exc)
                        or self._is_fts_write_corruption_error(exc)
                    ):
                        with self._lock:
                            self._raise_if_db_replaced()
                    # Corrupt FTS shadow tables fail every write via the sync triggers while canonical
                    # rows are intact: detach the derived indexes atomically and retry (never rebuild here).
                    if self._enter_fts_fail_open(exc, deadline=deadline, patience_s=patience_s):
                        continue
                    # What survives both checks is structural damage: quarantine.
                    if self._is_structural_corruption_error(exc):
                        self._halt_db_corrupt(exc)
                raise

    def _write_sql(
        self, sql: str, params: Any = (), *, many: bool = False, patience_s: Optional[float] = None,
    ) -> None:
        """Run one INSERT/UPDATE/DELETE through ``_execute_write``."""
        def _do(conn):
            (conn.executemany if many else conn.execute)(sql, params)
        self._execute_write(_do, patience_s=patience_s)

    def _write_rowcount(self, sql: str, params: Any = (), *, patience_s: Optional[float] = None) -> int:
        """Run one UPDATE/DELETE through ``_execute_write``; return rows changed
        (``SELECT changes()`` when the driver reports None / negative)."""
        def _do(conn):
            rowcount = conn.execute(sql, params).rowcount
            if rowcount is None or rowcount < 0:
                rowcount = conn.execute("SELECT changes()").fetchone()[0]
            return rowcount
        return self._execute_write(_do, patience_s=patience_s)

    def _read_one(self, sql: str, params: Any = ()) -> Optional[sqlite3.Row]:
        """``fetchone()`` of one read-only statement via ``_read_ctx``."""
        return self._read_retrying_ioerr(lambda conn: conn.execute(sql, params).fetchone())

    def _read_all(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        """``fetchall()`` of one read-only statement via ``_read_ctx``."""
        return self._read_retrying_ioerr(lambda conn: conn.execute(sql, params).fetchall())

    def _read_retrying_ioerr(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run an idempotent SELECT through ``_read_ctx``, retrying a transient SQLITE_IOERR.

        A warm ``mode=ro`` pooled reader can hit the same millisecond-wide WAL transition window as a
        read-only OPEN (#100436) when its statement executes or steps: a sibling process's checkpoint /
        WAL reset / frame flush surfaces ``disk I/O error`` because a read-only connection cannot rewrite
        the -shm index (#100871, WSL2 ext4-on-vhdx, multi-process). The window closes on its own, so
        the statement is replayed on the SAME connection within the read-only IOERR budget -- never
        closed and reopened (close() cancels this process's POSIX locks for every sibling connection),
        never quarantined (busy is not broken). A persistent IOERR exhausts the budget and propagates."""
        for attempt in range(_READ_ONLY_IOERR_RETRY_ATTEMPTS + 1):
            try:
                with self._read_ctx() as conn:
                    return fn(conn)
            except sqlite3.OperationalError as exc:
                if attempt >= _READ_ONLY_IOERR_RETRY_ATTEMPTS or _DISK_IO_ERROR_MARKER not in str(exc).lower():
                    note_storage_error(self.db_path, exc)
                    raise
                time.sleep(_READ_ONLY_IOERR_RETRY_BACKOFF_S)
            except sqlite3.DatabaseError as exc:
                # A reader is often the only observer (the sidebar poll on a store nobody is
                # writing to): publish structural damage so the list is not read as empty.
                note_storage_error(self.db_path, exc)
                raise

    def _ensure_db_file_generation(self) -> None:
        """Mint a once-per-file generation stamp (state_meta + application_id). First opener wins (INSERT
        OR IGNORE); application_id is written only while 0 so racers converge. PASSIVE checkpoint only.

        See #45383.
        """
        if self.read_only or self._conn is None:
            return
        token = uuid.uuid4().hex
        try:
            with self._lock:
                # Read first: the stamp is minted once per file, and a no-op INSERT OR IGNORE
                # still takes the write lock — under a sibling's transaction it blocked for the
                # busy timeout and the except below then dropped the token entirely. First
                # opener still wins via INSERT OR IGNORE; racers converge on the re-read.
                row = self._conn.execute(
                    "SELECT value FROM state_meta WHERE key = ?", (_STATE_DB_GENERATION_KEY,),
                ).fetchone()
                if not (row and row[0]):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO state_meta (key, value) VALUES (?, ?)",
                        (_STATE_DB_GENERATION_KEY, token),
                    )
                    row = self._conn.execute(
                        "SELECT value FROM state_meta WHERE key = ?",
                        (_STATE_DB_GENERATION_KEY,),
                    ).fetchone()
                if row and row[0]:
                    token = str(row[0])
                pragma_row = self._conn.execute("PRAGMA application_id").fetchone()
                current = int(pragma_row[0] or 0) if pragma_row else 0
                if current == 0:
                    current = (int(token[:8], 16) & 0x7FFFFFFF) or 1
                    self._conn.execute(f"PRAGMA application_id={current}")
                self._db_file_application_id = current
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
        except sqlite3.Error as exc:
            logger.debug("state.db generation stamp skipped: %s", exc)

    def _record_db_file_identity(self) -> None:
        """Snapshot inode plus the on-disk generation header when present."""
        self._db_file_identity = _stat_db_file_identity(self.db_path)
        self._db_sidecar_identity = _stat_sqlite_sidecar_identity(self.db_path)
        disk_id = _read_sqlite_application_id(self.db_path)
        if disk_id:
            self._db_file_application_id = disk_id
        elif self._conn is not None and not self._db_file_application_id:
            try:
                pragma_row = self._read_one("PRAGMA application_id")
            except sqlite3.Error:
                pragma_row = None
            if pragma_row and pragma_row[0]:
                self._db_file_application_id = int(pragma_row[0])

    def _db_file_was_replaced(self) -> bool:
        """True when the path no longer names the file this instance opened."""
        recorded = self._db_file_identity
        if recorded is not None and _stat_db_file_identity(self.db_path) != recorded:
            return True
        recorded_app = int(self._db_file_application_id or 0)
        if not recorded_app:
            return False
        # Header 0 = WAL not yet checkpointed, not a replace; a real replacement is nonzero.
        disk_app = _read_sqlite_application_id(self.db_path)
        return bool(disk_app and disk_app != recorded_app)

    def _wal_generation_was_lost(self) -> bool:
        """True when the WAL/SHM generation this handle opened is gone. Recorded
        generation: pure stat (no /proc walk on healthy writes). Empty identity
        (WAL appeared after open, or cleared by a clean close()): probe
        /proc/self/fd for deleted sidecars and adopt the current ones once clean."""
        recorded = self._db_sidecar_identity or {}
        base = os.fspath(self.db_path)
        if recorded:
            return any(
                _stat_db_file_identity(Path(base + suffix)) != ident for suffix, ident in recorded.items()
            )
        if not self._wal_active:  # no sidecar generation to lose; keep /proc off the hot path
            return False
        if sys.platform.startswith("linux"):
            watched = _watched_sqlite_sidecar_paths(self.db_path)
            try:
                for target, fd_path in _proc_fd_targets(os.getpid()):
                    canonical = _state_holders.canonical_sqlite_path(target)
                    if (" (deleted)" in target and canonical in watched
                            and _fd_is_truly_unlinked(fd_path, watched[canonical])):
                        return True
            except OSError:
                return False
        # Probe clean (or unavailable): adopt the current sidecar generation.
        current_identity = _stat_sqlite_sidecar_identity(self.db_path)
        if current_identity:
            self._db_sidecar_identity = current_identity
        return False

    def _halt_if_db_generation_changed(self) -> None:
        """Stop writes (logging once) when the file was replaced or its WAL/SHM generation
        is gone: never run in-file repair on a new generation, never keep committing on a
        split WAL. Both flags are sticky."""
        # A reopen resolves the PATH again — if the file at that path is no longer the one this instance
        # originally opened (out-of-band restore/cp/mv), reconnecting would write into the new generation
        # through stale WAL/shm assumptions (#89332). Refuse instead.
        if self._db_replaced or self._db_file_was_replaced():
            self._db_replaced = True
            self._disable_close_time_checkpoint()
            logger.error(_STATE_DB_REPLACED_MSG)
            raise StateDbReplacedError(_STATE_DB_REPLACED_MSG)
        if self._db_wal_generation_lost or self._wal_generation_was_lost():
            self._db_wal_generation_lost = True
            self._disable_close_time_checkpoint()
            try:
                self._capture_retired_generation("halt")
            except RetiredGenerationCaptureError as exc:
                logger.error(
                    "Could not capture the retired WAL generation of %s at halt: %s. close() retries "
                    "the capture and refuses to settle without it.", self.db_path, exc,
=======
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
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
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

<<<<<<< HEAD
        The quarantine keeps the retired frames from being checkpointed under wrong page numbers,
        but they live only in an unlinked inode that dies with this process's last descriptor, and
        the canonical DeletedWalGenerationError remediation is to stop the writers. Capturing at the
        first halt (or at close(), whichever sees the loss first) makes "preserve" outlive the
        process. Raises RetiredGenerationCaptureError; nothing is mutated on failure."""
        with self._retired_capture_lock:
            if self._retired_generation_capture is not None:
                return self._retired_generation_capture
            artifact = capture_retired_wal_generation(
                self.db_path, sidecar_identity=dict(self._db_sidecar_identity or {}), trigger=trigger,
            )
            self._retired_generation_capture = artifact
        logger.warning(
            "Captured the retired WAL generation of %s at %s to %s; read its manifest.json before deciding "
            "whether those frames belong on top of the file now at the path.", self.db_path, trigger, artifact,
        )
        return artifact

    def _raise_if_db_replaced(self) -> None:
        """Sticky-flag fast path (no log spam on every write), then the live probe."""
        self._raise_if_db_generation_flagged()
        self._halt_if_db_generation_changed()

    def _raise_if_db_generation_flagged(self) -> None:
        """The sticky replaced / lost-generation flags only (no filesystem syscalls)."""
        if self._db_replaced:
            raise StateDbReplacedError(_STATE_DB_REPLACED_MSG)
        if self._db_wal_generation_lost:
            raise DeletedWalGenerationError(_DELETED_WAL_GENERATION_MSG)

    def _db_generation_probe_clean(self) -> bool:
        """Lock-free healthy-path probe for writes: True only when the main file and every
        recorded sidecar still carry the identities recorded under ``self._lock`` and the
        header's application_id still matches. Pure (never adopts, never flags): any doubt
        returns False so the authoritative probe runs under the lock (see _execute_write_marked)."""
        if self._db_replaced or self._db_wal_generation_lost:
            return False
        try:
            recorded_main, recorded_sidecars = self._db_file_identity, self._db_sidecar_identity
            if recorded_main is None or (self._wal_active and not recorded_sidecars):
                return False
            if _stat_db_file_identity(self.db_path) != recorded_main:
                return False
            recorded_app = int(self._db_file_application_id or 0)
            if recorded_app:
                disk_app = _read_sqlite_application_id(self.db_path)
                if disk_app and disk_app != recorded_app:
                    return False
            base = os.fspath(self.db_path)
            return all(_stat_db_file_identity(Path(base + suffix)) == ident
                       for suffix, ident in (recorded_sidecars or {}).items())
        except Exception:
            return False

    @classmethod
    def _is_structural_corruption_error(cls, exc: BaseException) -> bool:
        """Bare SQLITE_CORRUPT/NOTADB with no FTS provenance: canonical B-tree/schema/freelist damage,
        never repairable from the live write path."""
        return (
            isinstance(exc, sqlite3.DatabaseError)
            and not isinstance(exc, StateDbCorruptError)
            and not cls._is_fts_write_corruption_error(exc)
            and classify_persistence_error(exc) == "corrupt"
        )

    def _corrupt_error(self, prefix: str = "") -> StateDbCorruptError:
        """Build the quarantine error for this handle (message assembled once)."""
        return StateDbCorruptError(f"{prefix}{_STATE_DB_CORRUPT_MSG} (cause: {self._db_corrupt_reason})")

    def _halt_db_corrupt(self, exc: BaseException) -> None:
        """Quarantine this handle and raise; never run in-file repair here."""
        self._db_corrupt = True
        self._db_corrupt_reason = str(exc)
        # Publish the profile-level state first: readiness, /api/status and the session list
        # endpoints read it, and every other handle in this process refuses writes on it.
        mark_storage_corrupt(self.db_path, exc)
        self._disable_close_time_checkpoint()
        logger.error(
            "state.db %s reported structural corruption outside the FTS "
            "indexes (%s); quarantining this handle: no further writes, no "
            "automatic reopen, no explicit WAL checkpoint at close. Stop the "
            "gateway and run `hermes sessions recover --source %s --inspect-only`.", self.db_path, exc,
            self.db_path,
        )
        err = self._corrupt_error()
        for attr in ("sqlite_errorcode", "sqlite_errorname"):
            if getattr(exc, attr, None) is not None:
                setattr(err, attr, getattr(exc, attr))
        raise err from exc

    def _disable_close_time_checkpoint(self) -> bool:
        """Best-effort SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE (Python 3.12+): sqlite3's
        close() otherwise runs the internal last-connection checkpoint that wrote
        the incident's pages under wrong page numbers (see StateDbCorruptError and
        the generation-loss halts).
        <3.12 has no setconfig, so a lost-generation handle is retired unclosed
        instead (see close()): closing its last descriptor could both run that
        checkpoint and discard committed data present only in an unlinked WAL."""
        flag = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
        conn = self._conn
        setconfig = getattr(conn, "setconfig", None)
        if flag is None or setconfig is None:
            # Same predicate as _close_time_checkpoint_configurable() plus the per-instance
            # getattr: __init__ binds no retirement capability when either half is missing,
            # and close() must agree with that decision or the lost handle would neither
            # setconfig nor pin.
            return False
||||||| dff103e05878
        The quarantine keeps the retired frames from being checkpointed under wrong page numbers,
        but they live only in an unlinked inode that dies with this process's last descriptor, and
        the canonical DeletedWalGenerationError remediation is to stop the writers. Capturing at the
        first halt (or at close(), whichever sees the loss first) makes "preserve" outlive the
        process. Raises RetiredGenerationCaptureError; nothing is mutated on failure."""
        with self._retired_capture_lock:
            if self._retired_generation_capture is not None:
                return self._retired_generation_capture
            artifact = capture_retired_wal_generation(
                self.db_path, sidecar_identity=dict(self._db_sidecar_identity or {}), trigger=trigger,
            )
            self._retired_generation_capture = artifact
        logger.warning(
            "Captured the retired WAL generation of %s at %s to %s; read its manifest.json before deciding "
            "whether those frames belong on top of the file now at the path.", self.db_path, trigger, artifact,
        )
        return artifact

    def _raise_if_db_replaced(self) -> None:
        """Sticky-flag fast path (no log spam on every write), then the live probe."""
        if self._db_replaced:
            raise StateDbReplacedError(_STATE_DB_REPLACED_MSG)
        if self._db_wal_generation_lost:
            raise DeletedWalGenerationError(_DELETED_WAL_GENERATION_MSG)
        self._halt_if_db_generation_changed()

    @classmethod
    def _is_structural_corruption_error(cls, exc: BaseException) -> bool:
        """Bare SQLITE_CORRUPT/NOTADB with no FTS provenance: canonical B-tree/schema/freelist damage,
        never repairable from the live write path."""
        return (
            isinstance(exc, sqlite3.DatabaseError)
            and not isinstance(exc, StateDbCorruptError)
            and not cls._is_fts_write_corruption_error(exc)
            and classify_persistence_error(exc) == "corrupt"
        )

    def _corrupt_error(self, prefix: str = "") -> StateDbCorruptError:
        """Build the quarantine error for this handle (message assembled once)."""
        return StateDbCorruptError(f"{prefix}{_STATE_DB_CORRUPT_MSG} (cause: {self._db_corrupt_reason})")

    def _halt_db_corrupt(self, exc: BaseException) -> None:
        """Quarantine this handle and raise; never run in-file repair here."""
        self._db_corrupt = True
        self._db_corrupt_reason = str(exc)
        # Publish the profile-level state first: readiness, /api/status and the session list
        # endpoints read it, and every other handle in this process refuses writes on it.
        mark_storage_corrupt(self.db_path, exc)
        self._disable_close_time_checkpoint()
        logger.error(
            "state.db %s reported structural corruption outside the FTS "
            "indexes (%s); quarantining this handle: no further writes, no "
            "automatic reopen, no explicit WAL checkpoint at close. Stop the "
            "gateway and run `hermes sessions recover --source %s --inspect-only`.", self.db_path, exc,
            self.db_path,
        )
        err = self._corrupt_error()
        for attr in ("sqlite_errorcode", "sqlite_errorname"):
            if getattr(exc, attr, None) is not None:
                setattr(err, attr, getattr(exc, attr))
        raise err from exc

    def _disable_close_time_checkpoint(self) -> bool:
        """Best-effort SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE (Python 3.12+): sqlite3's
        close() otherwise runs the internal last-connection checkpoint that wrote
        the incident's pages under wrong page numbers (see StateDbCorruptError and
        the generation-loss halts).
        <3.12 has no setconfig, so a lost-generation handle is retired unclosed
        instead (see close()): closing its last descriptor could both run that
        checkpoint and discard committed data present only in an unlinked WAL."""
        flag = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
        conn = self._conn
        setconfig = getattr(conn, "setconfig", None)
        if flag is None or setconfig is None:
            # Same predicate as _close_time_checkpoint_configurable() plus the per-instance
            # getattr: __init__ binds no retirement capability when either half is missing,
            # and close() must agree with that decision or the lost handle would neither
            # setconfig nor pin.
            return False
=======
        if not self.read_only:
            _ensure_reaper()  # before the acquire, so a reaper that cannot start leaves no handle open
        stack = _ExitStack()
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
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

<<<<<<< HEAD
    def _sleep_before_write_retry(self, deadline: float, patience_s: float) -> bool:
        """Sleep one jitter interval if the budget allows; True = retry, False = deadline passed. Small
        jitter for the first _WRITE_RETRY_SLOW_AFTER_S, then slow; never overshoots the deadline."""
        now = time.monotonic()
        if now >= deadline:
            return False
        slow = now - (deadline - patience_s) >= self._WRITE_RETRY_SLOW_AFTER_S
        jitter = random.uniform(*(
            (self._WRITE_RETRY_SLOW_MIN_S, self._WRITE_RETRY_SLOW_MAX_S) if slow
            else (self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S)
        ))
        time.sleep(min(jitter, max(deadline - now, 0.001)))
        return True

    def _foreign_state_db_holders(self) -> list[tuple[int, str]]:
        """Foreign processes holding this DB or its WAL sidecars (see hermes_state_holders)."""
        return _foreign_state_db_holders(self.db_path)

    def _quarantine_reason(self) -> Optional[str]:
        """Why this handle must not checkpoint or run in-file repair, or None. A corrupted image has
        torn B-trees; a replaced file or a deleted/replaced WAL generation would checkpoint under
        wrong page numbers into the main DB -- the shutdown-time cause of #105670. Precedence note:
        close() evaluates generation loss BEFORE calling this (and skips it entirely when lost —
        a lost generation settles through the capture path, not the quarantine advisory), while
        the halt path checks replaced first."""
        if self._db_corrupt:
            return f"structural corruption ({self._db_corrupt_reason})"
        if self._db_replaced:
            return "a replaced state.db file"
        if self._db_wal_generation_lost:
            return "a deleted WAL generation (split-brain)"
        return None

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint; never raises. PASSIVE never blocks writers;
        TRUNCATE corrupted B-trees on 65K+ page databases under exclusive-lock I/O pressure.

        Previous TRUNCATE strategy caused B-tree corruption on large databases (65K+ pages) due to the
        exclusive-lock I/O pressure from checkpointing thousands of frames at once (issue #45383).

        It runs on a dedicated connection WITHOUT ``self._lock``. A PASSIVE checkpoint needs no pause
        in this process's writes, but on macOS ``checkpoint_fullfsync`` makes it wait for the whole
        device's dirty data: under the writer lock, a busy disk stalled every Hermes writer (and the
        loop-thread waits behind them) for the checkpoint's duration. The fullfsync barrier itself is
        unchanged. One checkpoint runs at a time; a concurrent request skips to the next period.
        """
        if self._quarantine_reason() is not None:
            return
        try:
            with self._maintenance_conn(blocking=False) as conn:
                if conn is None:
                    return  # closed underneath the timer, or another maintenance pass is running
                result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and result[1] > 0:
                    logger.debug("WAL checkpoint: %d/%d pages checkpointed", result[2], result[1])
        except Exception as exc:
            logger.warning("WAL checkpoint (PASSIVE) failed: %s", exc)

    @contextmanager
    def _maintenance_conn(self, *, blocking: bool = True) -> Iterator[Optional[sqlite3.Connection]]:
        """The dedicated maintenance connection (periodic checkpoint, incremental FTS merge), held
        exclusively for the block and used WITHOUT ``self._lock``. Yields None when the handle is
        closed or read-only, or when *blocking* is False and another maintenance pass holds it."""
        if not self._checkpoint_lock.acquire(blocking=blocking):
            yield None
            return
        try:
            yield self._checkpoint_conn_for_current_writer()
        finally:
            self._checkpoint_lock.release()

    def _checkpoint_conn_for_current_writer(self) -> Optional[sqlite3.Connection]:
        """The checkpoint connection beside the live writer, (re)opened when the writer was replaced.
        Caller holds ``_checkpoint_lock``. Opening and closing descriptors on state.db happens under
        ``self._lock`` so it pairs with the WAL lock guard exactly like a writer reopen."""
        if self._checkpoint_conn is not None and self._checkpoint_conn_writer is self._conn:
            return self._checkpoint_conn
        with self._lock:
            if self._conn is None or self.read_only:
                return None
            stale, self._checkpoint_conn = self._checkpoint_conn, None
            if stale is not None:
                self._close_connection_quietly(stale)
            conn = self._open_writer_conn()
            if self._wal_lock_guard:
                _lockguard.hold(self.db_path, self._wal_lock_guard)  # its -shm/db descriptors too
            self._checkpoint_conn, self._checkpoint_conn_writer = conn, self._conn
            return conn

    def __enter__(self) -> "SessionDB":
        """``with SessionDB(path) as db:`` closes on exit; owners must release deterministically.

        Ownership of a SessionDB should be released explicitly. Historically an instance with a started
        token writer pinned ITSELF (bound-method writer target plus a strong ``atexit`` drain hook), so
        ``__del__`` never ran for exactly the instances that leaked descriptors (#88033). The writer now
        retires after an idle window and the atexit hook holds only a weak reference, so abandoned handles
        are eventually collectible — but "eventually, after the idle window and a GC cycle" is not a release
        policy. Call sites owning a handle are still expected to close it deterministically (see the
        ownership comments in ``run_agent.py`` and ``tui_gateway/methods_session.py``).
        """
||||||| dff103e05878
    def _sleep_before_write_retry(self, deadline: float, patience_s: float) -> bool:
        """Sleep one jitter interval if the budget allows; True = retry, False = deadline passed. Small
        jitter for the first _WRITE_RETRY_SLOW_AFTER_S, then slow; never overshoots the deadline."""
        now = time.monotonic()
        if now >= deadline:
            return False
        slow = now - (deadline - patience_s) >= self._WRITE_RETRY_SLOW_AFTER_S
        jitter = random.uniform(*(
            (self._WRITE_RETRY_SLOW_MIN_S, self._WRITE_RETRY_SLOW_MAX_S) if slow
            else (self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S)
        ))
        time.sleep(min(jitter, max(deadline - now, 0.001)))
        return True

    def _foreign_state_db_holders(self) -> list[tuple[int, str]]:
        """Foreign processes holding this DB or its WAL sidecars (see hermes_state_holders)."""
        return _foreign_state_db_holders(self.db_path)

    def _quarantine_reason(self) -> Optional[str]:
        """Why this handle must not checkpoint or run in-file repair, or None. A corrupted image has
        torn B-trees; a replaced file or a deleted/replaced WAL generation would checkpoint under
        wrong page numbers into the main DB -- the shutdown-time cause of #105670. Precedence note:
        close() evaluates generation loss BEFORE calling this (and skips it entirely when lost —
        a lost generation settles through the capture path, not the quarantine advisory), while
        the halt path checks replaced first."""
        if self._db_corrupt:
            return f"structural corruption ({self._db_corrupt_reason})"
        if self._db_replaced:
            return "a replaced state.db file"
        if self._db_wal_generation_lost:
            return "a deleted WAL generation (split-brain)"
        return None

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint; never raises. PASSIVE never blocks writers;
        TRUNCATE corrupted B-trees on 65K+ page databases under exclusive-lock I/O pressure.

        Previous TRUNCATE strategy caused B-tree corruption on large databases (65K+ pages) due to the
        exclusive-lock I/O pressure from checkpointing thousands of frames at once (issue #45383).
        """
        if self._quarantine_reason() is not None:
            return
        try:
            with self._lock:
                if self._conn is None:
                    return  # closed underneath the timer: nothing to checkpoint, nothing to re-guard
                if self._wal_lock_guard:
                    _lockguard.hold(self.db_path, self._wal_lock_guard)  # a -shm minted after open
                result = self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and result[1] > 0:
                    logger.debug("WAL checkpoint: %d/%d pages checkpointed", result[2], result[1])
        except Exception as exc:
            logger.warning("WAL checkpoint (PASSIVE) failed: %s", exc)

    def __enter__(self) -> "SessionDB":
        """``with SessionDB(path) as db:`` closes on exit; owners must release deterministically.

        Ownership of a SessionDB should be released explicitly. Historically an instance with a started
        token writer pinned ITSELF (bound-method writer target plus a strong ``atexit`` drain hook), so
        ``__del__`` never ran for exactly the instances that leaked descriptors (#88033). The writer now
        retires after an idle window and the atexit hook holds only a weak reference, so abandoned handles
        are eventually collectible — but "eventually, after the idle window and a GC cycle" is not a release
        policy. Call sites owning a handle are still expected to close it deterministically (see the
        ownership comments in ``run_agent.py`` and ``tui_gateway/methods_session.py``).
        """
=======
    def __enter__(self) -> "_SessionDB":
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- sessions --------------------------------------------------------------------------
    @_held
    def create_session(self, session_id: "str", source: "str", **kwargs: "Any") -> "str":
        from persistence.conversation.sessions import NewSession

<<<<<<< HEAD
        Drains queued token deltas first (the background writer needs the connection). Read-only connections
        never request a checkpoint. See #45383.
        When this instance is shared (opened via ``hermes_state_registry.acquire``), ``close()`` RELEASES one
        refcount instead of tearing down the connection: the registry owns the lifecycle and only closes on
        the final release (#90837). This prevents one caller's close from tearing down the writer connection
        that other callers in the same process are still using — while still letting legacy ``close()`` call
        sites return their reference instead of leaking it.
        """
        if self._shared_registry_owned:
            from hermes_state_registry import release
            release(self)
            return
        self._stop_token_writer()
        hook, self._token_atexit_hook = self._token_atexit_hook, None
        if hook is not None:
            atexit.unregister(hook)
        # Closed flag first: an in-flight reader then closes its own connection.
        with self._read_conns_lock:
            self._read_conns_closed = True
        while self._evict_one_idle_read_conn():
            pass
        with self._checkpoint_lock, self._lock:  # an in-flight periodic checkpoint finishes first
            checkpoint_conn, self._checkpoint_conn = self._checkpoint_conn, None
            self._checkpoint_conn_writer = None
            if self._conn:
                generation_lost = not self.read_only and (
                    self._db_wal_generation_lost
                    or (bool(self._db_sidecar_identity) and self._wal_generation_was_lost())
                )
                # Loss is settled here, not at exit: the unlinked WAL inode dies with this process's
                # last descriptor (the capture raises and the handle stays open when it fails).
                retire_without_close = generation_lost and self._settle_lost_generation_locked()
                quarantine_reason = None if generation_lost else self._quarantine_reason()
                if quarantine_reason is not None:
                    logger.warning(
                        "Skipping the close-time WAL checkpoint for %s: this "
                        "handle observed %s. Take a snapshot of state.db, -wal and -shm "
                        "before restarting, then run `hermes sessions recover --source %s --inspect-only`.",
                        self.db_path, quarantine_reason, self.db_path,
                    )
                elif not self.read_only and not generation_lost:  # PASSIVE, not TRUNCATE (see docstring)
                    try:
                        # Every cron run_agent opens+closes a transient SessionDB, so a TRUNCATE here fires
                        # a full WAL reset many times/hour, racing the gateway's long-lived writer on large
                        # WAL databases and tearing hot B-tree pages -- the #45383 corruption this class's
                        # own periodic checkpoint was already made PASSIVE to avoid. TRUNCATE belongs only
                        # on a sole-opener/quiescent connection.
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except Exception as exc:
                        logger.debug("WAL checkpoint (PASSIVE) at close failed: %s", exc)
                if checkpoint_conn is not None and not retire_without_close:
                    # Closed while the guard still holds, like a read-pool connection: its close
                    # cannot cancel this handle's protection of the live generation.
                    self._close_connection_quietly(checkpoint_conn)
                _lockguard.release(self._wal_lock_guard)  # before the close: see release()
                if retire_without_close:
                    self._pin_connection(self._conn)
                    if checkpoint_conn is not None:
                        self._retire_connection(checkpoint_conn)  # its descriptors stay with the writer's
                    self._conn = None
                else:
                    conn, self._conn = self._conn, None
                    self._close_connection_quietly(conn)
                    # Only a clean close ends the generation; retain the recorded
                    # identity when retiring an unsafe handle.
                    self._db_sidecar_identity = {}
            elif checkpoint_conn is not None:
                self._close_connection_quietly(checkpoint_conn)
        self._read_budget.unregister(self)  # idempotent: a never-registered (failed-init) handle is a no-op
||||||| dff103e05878
        Drains queued token deltas first (the background writer needs the connection). Read-only connections
        never request a checkpoint. See #45383.
        When this instance is shared (opened via ``hermes_state_registry.acquire``), ``close()`` RELEASES one
        refcount instead of tearing down the connection: the registry owns the lifecycle and only closes on
        the final release (#90837). This prevents one caller's close from tearing down the writer connection
        that other callers in the same process are still using — while still letting legacy ``close()`` call
        sites return their reference instead of leaking it.
        """
        if self._shared_registry_owned:
            from hermes_state_registry import release
            release(self)
            return
        self._stop_token_writer()
        hook, self._token_atexit_hook = self._token_atexit_hook, None
        if hook is not None:
            atexit.unregister(hook)
        # Closed flag first: an in-flight reader then closes its own connection.
        with self._read_conns_lock:
            self._read_conns_closed = True
        while self._evict_one_idle_read_conn():
            pass
        with self._lock:
            if self._conn:
                generation_lost = not self.read_only and (
                    self._db_wal_generation_lost
                    or (bool(self._db_sidecar_identity) and self._wal_generation_was_lost())
                )
                # Loss is settled here, not at exit: the unlinked WAL inode dies with this process's
                # last descriptor (the capture raises and the handle stays open when it fails).
                retire_without_close = generation_lost and self._settle_lost_generation_locked()
                quarantine_reason = None if generation_lost else self._quarantine_reason()
                if quarantine_reason is not None:
                    logger.warning(
                        "Skipping the close-time WAL checkpoint for %s: this "
                        "handle observed %s. Take a snapshot of state.db, -wal and -shm "
                        "before restarting, then run `hermes sessions recover --source %s --inspect-only`.",
                        self.db_path, quarantine_reason, self.db_path,
                    )
                elif not self.read_only and not generation_lost:  # PASSIVE, not TRUNCATE (see docstring)
                    try:
                        # Every cron run_agent opens+closes a transient SessionDB, so a TRUNCATE here fires
                        # a full WAL reset many times/hour, racing the gateway's long-lived writer on large
                        # WAL databases and tearing hot B-tree pages -- the #45383 corruption this class's
                        # own periodic checkpoint was already made PASSIVE to avoid. TRUNCATE belongs only
                        # on a sole-opener/quiescent connection.
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except Exception as exc:
                        logger.debug("WAL checkpoint (PASSIVE) at close failed: %s", exc)
                _lockguard.release(self._wal_lock_guard)  # before the close: see release()
                if retire_without_close:
                    self._pin_connection(self._conn)
                    self._conn = None
                else:
                    conn, self._conn = self._conn, None
                    self._close_connection_quietly(conn)
                    # Only a clean close ends the generation; retain the recorded
                    # identity when retiring an unsafe handle.
                    self._db_sidecar_identity = {}
        self._read_budget.unregister(self)  # idempotent: a never-registered (failed-init) handle is a no-op
=======
        origin_json = kwargs.pop("origin_json", None)
        if origin_json is not None:
            kwargs["origin"] = _json.loads(origin_json)
        return self._store.sessions.create(NewSession(session_id=session_id, source=source, **kwargs))
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

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

<<<<<<< HEAD
    def set_meta(self, key: str, value: str, *, cursor: Optional[sqlite3.Cursor] = None) -> None:
        """Upsert state_meta[key]; with ``cursor`` the write is inline (the caller already holds a
        transaction — nesting BEGIN IMMEDIATE would deadlock)."""
        from hermes_state_incognito import private_write
        if private_write((key, value)):
            return
        sql = (
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        if cursor is not None:
            cursor.execute(sql, (key, value))
        else:
            self._write_sql(sql, (key, value))
||||||| dff103e05878
    def set_meta(self, key: str, value: str, *, cursor: Optional[sqlite3.Cursor] = None) -> None:
        """Upsert state_meta[key]; with ``cursor`` the write is inline (the caller already holds a
        transaction — nesting BEGIN IMMEDIATE would deadlock)."""
        sql = (
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        if cursor is not None:
            cursor.execute(sql, (key, value))
        else:
            self._write_sql(sql, (key, value))
=======
    @_held
    def set_session_title(self, session_id: "str", title: "str") -> "bool":
        return self._store.sessions.set_title(session_id, title)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

<<<<<<< HEAD
    def retag_kanban_worker_sessions(self, workspaces_root: str) -> int:
        """Retag legacy kanban worker rows from ``cli`` to ``kanban`` by cwd under the board's workspaces
        root; gated once per root via state_meta. Returns rows retagged."""
        prefix = str(workspaces_root).rstrip("/\\")
        if not prefix:
            return 0
        gate = f"kanban_worker_source_retagged:{prefix}"
        if self.get_meta(gate) == "1":
            return 0
        def _do(conn):
            esc = _escape_like(prefix)
            cursor = conn.execute(
                "UPDATE sessions SET source = 'kanban' "
                "WHERE source = 'cli' AND (cwd = ? OR cwd LIKE ? ESCAPE '\\' "
                "OR cwd LIKE ? ESCAPE '\\')",
                (prefix, f"{esc}/%", f"{esc}\\\\%"),
            )
            # rowcount BEFORE set_meta reuses this cursor for its INSERT.
            retagged = cursor.rowcount or 0
            self.set_meta(gate, "1", cursor=cursor)
            return retagged
        return self._execute_write(_do, skip_result=0)
||||||| dff103e05878
    def retag_kanban_worker_sessions(self, workspaces_root: str) -> int:
        """Retag legacy kanban worker rows from ``cli`` to ``kanban`` by cwd under the board's workspaces
        root; gated once per root via state_meta. Returns rows retagged."""
        prefix = str(workspaces_root).rstrip("/\\")
        if not prefix:
            return 0
        gate = f"kanban_worker_source_retagged:{prefix}"
        if self.get_meta(gate) == "1":
            return 0
        def _do(conn):
            esc = _escape_like(prefix)
            cursor = conn.execute(
                "UPDATE sessions SET source = 'kanban' "
                "WHERE source = 'cli' AND (cwd = ? OR cwd LIKE ? ESCAPE '\\' "
                "OR cwd LIKE ? ESCAPE '\\')",
                (prefix, f"{esc}/%", f"{esc}\\\\%"),
            )
            # rowcount BEFORE set_meta reuses this cursor for its INSERT.
            retagged = cursor.rowcount or 0
            self.set_meta(gate, "1", cursor=cursor)
            return retagged
        return self._execute_write(_do)
=======
    @_held
    def resolve_session_by_title(self, title: "str") -> "Optional[str]":
        return self._store.sessions.resolve_by_title(title)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

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
