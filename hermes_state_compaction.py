"""One-shot "compact state.db at the next gateway start" request.

``hermes sessions optimize`` refuses while any process holds state.db, and a running gateway always
does, so a large store can never be compacted from inside (or beside) a live gateway. Automatic
VACUUM in ``maybe_auto_prune_and_vacuum`` skips for the same reason and additionally needs a fresh
prune, so after a bulk delete the freed pages stay on disk indefinitely.

``hermes sessions optimize --at-next-start`` only records intent in a sidecar file next to the store
(``state.db.compact-at-start.json``), which works while the gateway runs. The next gateway start calls
:func:`run_pending_compaction` right after it wins the PID-file claim and before adapters, cron, the
housekeeping worker or any SessionDB handle exist. It runs the exact ``hermes sessions optimize`` work
(:meth:`SessionDB.vacuum`: FTS merge + VACUUM + TRUNCATE checkpoint), then clears the request.

It only ever skips (request kept, WARNING logged) when another process holds the store, a release
promotion is waiting for this gateway's acknowledgement, or free disk is short; it never raises.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

REQUEST_SUFFIX = ".compact-at-start.json"
# Attempts are counted BEFORE the rewrite starts, so a start killed mid-VACUUM (supervisor stop,
# crash) still counts; after this many the request is dropped instead of retried on every boot.
MAX_ATTEMPTS = 3
LEASE_PHASE = "state_db_compact_at_start"
# Startup-watchdog lease per renewal (the watchdog clamps each call to 900s).
_LEASE_S = 900.0
_RENEW_INTERVAL_S = 60.0
# Headroom beyond the estimated rewrite footprint.
_DISK_MARGIN_BYTES = 1 << 30


def request_path(db_path) -> Path:
    db_path = Path(db_path)
    return db_path.with_name(db_path.name + REQUEST_SUFFIX)


def read_request(db_path) -> Optional[Dict[str, Any]]:
    """The pending request, or None. An unreadable marker still counts as a request."""
    path = request_path(db_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"attempts": 0}
    return data if isinstance(data, dict) else {"attempts": 0}


def request_compaction(db_path, *, requested_by: str = "cli") -> Path:
    """Record a one-shot request (idempotent: re-requesting resets the attempt count)."""
    from utils import atomic_json_write

    path = request_path(db_path)
    atomic_json_write(path, {
        "version": 1,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": requested_by,
        "attempts": 0,
    })
    return path


def cancel_request(db_path) -> bool:
    """Remove a pending request; True when one existed."""
    try:
        request_path(db_path).unlink()
        return True
    except FileNotFoundError:
        return False


def _write_attempts(db_path, request: Dict[str, Any], attempts: int) -> None:
    from utils import atomic_json_write

    atomic_json_write(request_path(db_path), {**request, "attempts": attempts,
                                              "last_attempt_at": datetime.now(timezone.utc).isoformat()})


def _sqlite_temp_dir() -> Path:
    """Where SQLite's unix VFS puts VACUUM's temporary copy (same search order as os_unix.c)."""
    for candidate in (os.environ.get("SQLITE_TMPDIR"), os.environ.get("TMPDIR"), "/var/tmp", "/usr/tmp", "/tmp"):  # no-tmp: ok — mirrors SQLite's temp-dir search to size VACUUM, not scratch use
        if candidate and os.path.isdir(candidate):
            return Path(candidate)
    return Path(".")


def _disk_shortfall(db_path: Path, live_bytes: int) -> Optional[str]:
    """None when free disk covers the rewrite, else a human-readable shortfall.

    VACUUM builds a compacted copy in SQLite's temp directory, then writes it back through the WAL
    before the TRUNCATE checkpoint, so the peak extra footprint is about twice the live data: once
    in the temp dir and once in ``state.db-wal``.
    """
    temp_dir = _sqlite_temp_dir()
    try:
        same_device = os.stat(temp_dir).st_dev == os.stat(db_path.parent).st_dev
    except OSError:
        same_device = True
    needs = {db_path.parent: live_bytes * (2 if same_device else 1) + _DISK_MARGIN_BYTES}
    if not same_device:
        needs[temp_dir] = live_bytes + _DISK_MARGIN_BYTES
    for directory, need in needs.items():
        free = shutil.disk_usage(directory).free
        if free < need:
            return f"{directory} has {free / 2**30:.1f} GiB free, needs ~{need / 2**30:.1f} GiB"
    return None


def _file_marks(db_path: Path) -> tuple:
    marks = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(f"{db_path}{suffix}")
            marks.append((st.st_size, st.st_mtime_ns))
        except OSError:
            marks.append(None)
    return tuple(marks)


class _ProgressLease:
    """Renew the startup-watchdog lease while the rewrite shows real progress.

    VACUUM and the checkpoint are I/O-bound with near-zero CPU, which the watchdog's CPU fallback
    reads as a parked deadlock. Evidence is the SQLite VM advancing (progress handler) or the store
    or its WAL changing on disk (the checkpoint runs outside the VM). Without evidence the lease is
    not renewed, so a genuinely wedged rewrite still lets the watchdog fire. On systemd, the same
    tick extends the start timeout for Type=notify units.
    """

    def __init__(self, db_path: Path, conn) -> None:
        self._db_path = db_path
        self._conn = conn
        self._steps = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="state-db-compact-lease")

    def _on_progress(self) -> int:
        self._steps += 1
        return 0

    @staticmethod
    def renew() -> None:
        from hermes_startup_watchdog import report_startup_progress

        report_startup_progress(_LEASE_S, phase=LEASE_PHASE)
        try:
            from gateway.systemd_notify import notify

            notify(f"EXTEND_TIMEOUT_USEC={int(_LEASE_S * 1_000_000)}")
        except Exception:
            pass

    def _run(self) -> None:
        last = (self._steps, _file_marks(self._db_path))
        while not self._stop.wait(_RENEW_INTERVAL_S):
            current = (self._steps, _file_marks(self._db_path))
            if current != last:
                self.renew()
            last = current

    def __enter__(self) -> "_ProgressLease":
        self.renew()
        try:
            self._conn.set_progress_handler(self._on_progress, 10_000)
        except Exception:
            logger.debug("state.db compaction: progress handler unavailable", exc_info=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        try:
            self._conn.set_progress_handler(None, 0)
        except Exception:
            pass


def run_pending_compaction(db_path) -> Dict[str, Any]:
    """Honor a pending request once. Never raises; returns ``{"status": ...}`` for logs and tests."""
    try:
        return _run_pending_compaction(Path(db_path))
    except Exception as exc:
        logger.warning("state.db compaction at startup failed; request kept for the next start: %s", exc)
        return {"status": "error", "error": str(exc)}


def _run_pending_compaction(db_path: Path) -> Dict[str, Any]:
    request = read_request(db_path)
    if request is None:
        return {"status": "none"}
    if not db_path.exists():
        cancel_request(db_path)
        logger.info("state.db compaction request dropped: %s does not exist", db_path)
        return {"status": "no_store"}
    try:
        attempts = max(0, int(request.get("attempts") or 0))
    except (TypeError, ValueError):
        attempts = 0
    if attempts >= MAX_ATTEMPTS:
        cancel_request(db_path)
        logger.warning(
            "state.db compaction request dropped after %d interrupted attempt(s); re-request with "
            "`hermes sessions optimize --at-next-start` once the cause is understood", attempts)
        return {"status": "abandoned", "attempts": attempts}
    # A promotion waits a bounded time for this gateway to acknowledge its release; a multi-minute
    # rewrite here would fail the update and can trigger the guardian's rollback mid-VACUUM.
    if (db_path.parent / "release-txn.json").exists():
        logger.warning("state.db compaction deferred: a release update is waiting for this gateway's "
                       "acknowledgement; the request stays pending for the next start")
        return {"status": "deferred_release"}

    from hermes_state_holders import foreign_state_db_holders, in_process_state_db_holders

    holders = foreign_state_db_holders(db_path)
    if holders:
        logger.warning(
            "state.db compaction deferred: %d other process(es) hold the store (%s); the request "
            "stays pending for the next start", len(holders),
            ", ".join(f"{pid}:{target}" for pid, target in holders[:3]))
        return {"status": "deferred_holders", "holders": len(holders)}

    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    try:
        pragmas = db._page_pragmas(("page_count", "freelist_count", "page_size"),
                                   "state.db compaction: could not read page counts: %s")
        if pragmas is None:
            raise RuntimeError("could not read page counts")
        page_count, freelist, page_size = pragmas
        live_bytes = (page_count - freelist) * page_size
        shortfall = _disk_shortfall(db_path, live_bytes)
        if shortfall:
            logger.warning("state.db compaction skipped: not enough free disk (%s); the request "
                           "stays pending for the next start", shortfall)
            return {"status": "skipped_disk", "detail": shortfall}
        # Re-check right before the rewrite: nothing in this process may hold another generation,
        # and a process that opened the store since the first scan must not be rewritten under.
        holders = foreign_state_db_holders(db_path) + in_process_state_db_holders(db_path, exclude=db)
        if holders:
            logger.warning("state.db compaction deferred: %d holder(s) appeared before the rewrite; "
                           "the request stays pending", len(holders))
            return {"status": "deferred_holders", "holders": len(holders)}

        _write_attempts(db_path, request, attempts + 1)
        before = os.path.getsize(db_path)
        logger.info(
            "state.db compaction requested at %s: rewriting %.1f GiB (%.0f%% free pages) before "
            "the gateway starts; this can take several minutes on a large store",
            request.get("requested_at") or "unknown time", before / 2**30,
            100.0 * freelist / page_count if page_count else 0.0)
        started = time.monotonic()
        with _ProgressLease(db_path, db._conn):
            optimized = db.vacuum()
        after = os.path.getsize(db_path)
    finally:
        db.close()
    cancel_request(db_path)
    elapsed = time.monotonic() - started
    logger.info(
        "state.db compaction complete in %.0fs: %.1f GiB -> %.1f GiB (%d FTS index(es) merged)",
        elapsed, before / 2**30, after / 2**30, optimized)
    return {"status": "compacted", "before": before, "after": after, "seconds": elapsed,
            "fts_optimized": optimized}
