"""Running-process checkpoint persistence and PID-safe recovery."""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.process_registry")


class ProcessCheckpointMixin:
    # ----- Checkpoint (crash recovery) -----

    def _write_checkpoint(self, extra_entries: Optional[List[Dict[str, Any]]] = None):
        """Write running process metadata to the checkpoint file atomically."""
        from tools.process_registry import _checkpoint_path, _CHECKPOINT_FIELDS
        from tools.process_registry_results import completed_result_record

        try:
            with self._lock:
                entries = []
                for s in {**self._finished, **self._running}.values():
                    if s.exited:
                        if s.id in self._running or s._result_persist_failed:
                            record = completed_result_record(s)
                            record["result_observed"] = False
                            entries.append({"session_id": s.id, "completed_result": record})
                        continue
                    # Backfill the start time so recovery can detect PID recycling
                    # even for sessions spawned before this field existed.
                    if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                        s.host_start_time = self._safe_host_start_time(s.pid)
                    entry = {"session_id": s.id, **{f: getattr(s, f) for f in _CHECKPOINT_FIELDS}}
                    # Redact inline credentials before persisting (~/.hermes/processes.json).
                    # Recovery uses command only for display (adoption re-validates the
                    # PID, never re-runs it), so masking is lossless.
                    # See #77484.
                    entry["command"] = redact_sensitive_text(s.command, code_file=True)
                    entry["owner_task_id"] = s.owner_task_id or s.task_id
                    entries.append(entry)
                if extra_entries:
                    tracked_ids = {item.get("session_id") for item in entries}
                    entries.extend(item for item in extra_entries if item.get("session_id") not in tracked_ids)
                # Malformed originals have no replacement proof. Keep them even
                # when their claimed ID collides with a healthy tracked entry.
                entries.extend(self._unresolved_checkpoint_entries)
                from utils import atomic_json_write
                atomic_json_write(_checkpoint_path(), entries)
        except Exception as e:
            logger.debug("Failed to write checkpoint file: %s", e, exc_info=True)

    def recover_from_checkpoint(self) -> int:
        """On gateway startup, probe PIDs from the checkpoint file; returns how many
        were recovered as detached sessions."""
        from tools.process_registry import _checkpoint_path

        if not _checkpoint_path().exists():
            return 0
        try:
            entries = json.loads(_checkpoint_path().read_text(encoding="utf-8"))
        except Exception:
            return 0
        if not isinstance(entries, list):
            return 0  # Unparseable container is left untouched.
        recovered = 0
        self._unresolved_checkpoint_entries = []
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid process checkpoint entry")
                if "completed_result" in entry:
                    from tools.process_registry_results import restore_checkpoint_result
                    # Completed fallbacks are never PID candidates, even if bad.
                    session = restore_checkpoint_result(entry["completed_result"])
                    if session.id != entry.get("session_id"):
                        raise ValueError("Completed checkpoint identity mismatch")
                    with self._lock:
                        self._finished[session.id] = session
                        self._result_generation += 1
                else:
                    recovered += self._recover_live_checkpoint_entry(entry)
            except (ValueError, KeyError, TypeError, AttributeError):
                # Preserve the original through subsequent checkpoint writes,
                # without withholding unrelated healthy recovery and watchers.
                self._unresolved_checkpoint_entries.append(entry)
                logger.warning("Retaining malformed process checkpoint entry", exc_info=True)
        self._write_checkpoint()
        # HERMES-123: a persisted deadline is only enforceable if its expiry thread is restarted
        # here. Without this, a recovered process keeps deadline_at set and never expires.
        with self._lock:
            deadline_sessions = list(self._running.values())
        for session in deadline_sessions:
            self._start_deadline(session)
        return recovered

    def _recover_live_checkpoint_entry(self, entry) -> bool:
        from tools.process_registry import (
            ProcessSession, _CHECKPOINT_FIELDS, _CHECKPOINT_DEFAULTS,
            _WATCHER_ROUTE_KEYS, _stop_systemd_unit,
        )

        pid, pid_scope = entry.get("pid"), entry.get("pid_scope", "host")
        if not pid:
            return False
        # A multiplexed process registry adopts each live session only once.
        with self._lock:
            if entry.get("session_id") in self._running:
                return False
        if pid_scope != "host":  # in-sandbox PIDs mean nothing once the env handle is gone
            logger.info(
                "Skipping recovery for non-host process: %s (pid=%s, scope=%s)",
                entry.get("command", "unknown")[:60], pid, pid_scope)
            return False
        # Alive AND the same process: across a restart the kernel may have
        # recycled the PID onto a stranger, and adopting it would let a later
        # kill tree-kill e.g. a browser.
        if not self._host_pid_is_ours(pid, entry.get("host_start_time")):
            if self._is_host_pid_alive(pid):
                logger.info(
                    "Not recovering session %s: pid %d is alive but its "
                    "start time no longer matches — PID was recycled onto "
                    "an unrelated process; refusing to adopt it.",
                    entry.get("session_id", "?"), pid)
            systemd_unit = entry.get("systemd_unit", "")
            if systemd_unit and not _stop_systemd_unit(systemd_unit):
                logger.warning(
                    "Could not reap persisted scope %s for dead wrapper pid %s; "
                    "retaining checkpoint entry for the next startup",
                    systemd_unit, pid)
                self._unresolved_checkpoint_entries.append(entry)
            return False
        fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
        fields.update(
            command=entry.get("command", "unknown"),
            owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
            started_at=entry.get("started_at", time.time()))
        # detached: can't read output, but can report status + kill
        session = ProcessSession(id=entry["session_id"], detached=True, **fields)
        with self._lock:
            self._running[session.id] = session

        logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)
        # Re-enqueue watcher so gateway can resume notifications
        if session.watcher_interval > 0:
            self.pending_watchers.append({
                "session_id": session.id,
                "check_interval": session.watcher_interval,
                "session_key": session.session_key,
                **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
                "notify_on_complete": session.notify_on_complete,
                "parent_session_id": session.parent_session_id,
            })
        return True
