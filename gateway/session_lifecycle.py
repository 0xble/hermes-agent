"""SessionStore explicit suspension, crash-recovery markers, pruning and shared clock/id helpers."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from gateway.session import SessionEntry, SessionSource

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.session")


def _now() -> datetime:
    """Return the current local time."""
    return datetime.now()


def _new_session_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(value) -> Optional[datetime]:
    """``datetime.fromisoformat`` that returns None for empty/malformed input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# Auto-continue freshness window (1 hour) after the ``resume_pending`` mark; ``gateway/run.py``
# bridges config.yaml ``agent.gateway_auto_continue_freshness`` into the env var at startup.
_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT = 60 * 60


def auto_continue_freshness_window() -> float:
    """Resume-scheduler freshness window; stale automation never discards the transcript."""
    raw = os.environ.get("HERMES_AUTO_CONTINUE_FRESHNESS")
    try:
        return float(raw) if raw else float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    except (TypeError, ValueError):
        return float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)


class SessionLifecycleMixin:
    """SessionStore explicit boundaries and crash-recovery markers."""

    def _is_session_ended_in_db(self, session_id: str) -> bool:
        """True iff state.db has this session with a non-null end_reason (same staleness test as
        ``_prune_stale_sessions_locked``; no DB/row or DB error -> False). Lets routing self-heal a
        session ended while the gateway stays alive. Store resolved from the owning profile.

        Used by ``get_or_create_session`` to self-heal at routing time: ``_prune_stale_sessions_locked``
        only runs at startup, so a session ended in the DB while the gateway stays alive (any path that
        finalizes the row without clearing sessions.json) would otherwise be reused as a live routing key
        and silently swallow every subsequent message until the next restart (#54878 — the live-gateway
        variant of #52804/FM9). DB errors are non-fatal — never block routing on a failed lookup.
        The store is resolved from the row's owning profile rather than the ambient scope: an unscoped
        background writer keeps its own copy of the same session, and comparing against that copy reports a
        live session as ended (#66887).
        """
        db = self._db_for_session_id(session_id)
        if not db or not session_id:
            return False
        try:
            row = db.get_session(session_id)
        except Exception:
            return False
        return bool(row is not None and row.get("end_reason") is not None)

    def _route_reset_reason(self, entry: SessionEntry) -> Optional[str]:
        """Only explicit suspension replaces a routed conversation; time never does."""
        return "suspended" if entry.suspended else None

    def _update_entry(self, session_key: str, mutate) -> bool:
        """Apply ``mutate(entry)`` under ``_lock`` and full-save; False when the entry is missing
        or *mutate* returned False (nothing to persist)."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or mutate(entry) is False:
                return False
            self._save()
            return True

    def _update_all_entries_locked(self, mutate) -> int:
        """Apply ``mutate(entry) -> bool`` to every entry under ``_lock``; save once if any
        returned True. Returns the count that did."""
        with self._lock:
            self._ensure_loaded_locked()
            changed = sum(1 for entry in self._entries.values() if mutate(entry))
            if changed:
                self._save()
        return changed

    def suspend_session(self, session_key: str) -> bool:
        """Mark a session suspended so it auto-resets on next access (/stop). True if it existed.

        Used by ``/stop`` to prevent stuck sessions from being resumed after a gateway restart (#7536).
        """
        def suspend(entry):
            self._settle_restart_reset(entry)
            entry.suspended = True
        return self._update_entry(session_key, suspend)

    def _settle_restart_reset(self, entry):
        """Explicit user boundaries cancel linked execution before replacing its only marker."""
        link = entry.restart_inbox_link
        if not link or link.get("protocol") != 1 or link.get("mode") == "delivered":
            return
        from gateway.restart_inbox import linked_row, transition_link
        row = linked_row(link)
        if row["state"] in ("delivered", "abandoned"):
            return
        owns = (row["owner_pid"], row["owner_started_at"]) == (link["owner_pid"], link["owner_started_at"])
        if not transition_link(link, "abandoned", recovery=not owns):
            raise RuntimeError("Cannot cancel a restart inbox claim owned by another process")

    def _set_turn_marker_locked(self, session_key: str, entry: SessionEntry, token, started_at,
                                *, restart_link=None, settled_at=None) -> None:
        """Persist the active-turn pair BEFORE publishing it in memory, so a failed write can
        neither leak an unowned token nor drop a live one. Lock held."""
        candidate = entry.to_dict()
        candidate["active_turn_token"] = token
        candidate["active_turn_started_at"] = _iso(started_at)
        candidate["restart_inbox_link"] = restart_link
        candidate["restart_inbox_settled_at"] = settled_at
        if started_at is not None:
            # Keeps the legacy 120s startup heuristic working for an older binary during a rolling
            # downgrade/upgrade window.
            candidate["updated_at"] = started_at.isoformat()
        strict = bool(restart_link or entry.restart_inbox_link)
        self._save_entry(session_key, entry_data=candidate, lock_held=True,
                         **({"require_primary": True} if strict else {}))
        entry.active_turn_token = token
        entry.active_turn_started_at = started_at
        entry.restart_inbox_link = restart_link
        entry.restart_inbox_settled_at = settled_at
        if started_at is not None:
            entry.updated_at = started_at

    def mark_turn_active(self, session_key: str, *, restart_claim=None) -> Optional[str]:
        """Persist exact ownership of the running agent turn; returns the opaque token for
        :meth:`clear_turn_active`. Re-marking replaces the previous token so a stale asynchronous
        unwind cannot clear a newer turn."""
        token = uuid.uuid4().hex
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None:
                return None
            from agent.turn_context import RequiredInputPersistenceError
            from gateway.restart_inbox import linked_row
            link = restart_claim
            if link:
                previous = entry.restart_inbox_link
                if previous and previous.get("queue_id") != link["queue_id"]:
                    raise RequiredInputPersistenceError("Another restart input still owns this session")
                row = linked_row(link)
                if (link["session_key"] != session_key or row["state"] != "attempting"
                        or (row["owner_pid"], row["owner_started_at"]) !=
                        (link["owner_pid"], link["owner_started_at"])):
                    raise RequiredInputPersistenceError("Restart claim no longer owns this turn")
                link = dict(link, session_id=entry.session_id, mode="active", turn_token=token)
            elif entry.restart_inbox_link and entry.restart_inbox_link.get("mode") != "delivered":
                from gateway.restart_inbox import adopt_continuation
                if entry.restart_inbox_link.get("mode") != "continuation":
                    raise RequiredInputPersistenceError("Restart recovery is awaiting reconciliation")
                link = adopt_continuation(entry.restart_inbox_link)
                if link is None:
                    raise RequiredInputPersistenceError("Restart continuation ownership unavailable")
                link = dict(link, mode="active", turn_token=token)
            self._set_turn_marker_locked(session_key, entry, token, _now(), restart_link=link)
        return token

    def clear_turn_active(self, session_key: str, token: str) -> bool:
        """Compare-and-swap clear an active-turn marker; ``False`` when the entry disappeared or a
        newer turn owns it."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or entry.active_turn_token != token:
                return False
            if entry.restart_inbox_link:
                from gateway.restart_inbox import transition_link
                if not transition_link(entry.restart_inbox_link, "delivered"):
                    return False
            settled_at = _now().isoformat() if entry.restart_inbox_link else entry.restart_inbox_settled_at
            self._set_turn_marker_locked(session_key, entry, None, None, settled_at=settled_at)
        return True

    def reconcile_restart_inbox(self, db_paths, *, running_keys=()):
        """Partition linked work before either startup consumer. Unknown evidence parks both."""
        from pathlib import Path
        from gateway.restart_inbox import (
            linked_row, read_rows, transition_link, _owner_alive, ambiguous_unlinked_attempt,
        )

        paths = {str(Path(path).resolve()) for path in db_paths}
        blocked = {path: set() for path in paths}
        with self._lock:
            self._ensure_loaded_locked()
            rows_by_path = {
                path: read_rows(path) if Path(path).exists() else [] for path in paths
            }
            # Old attempted work lacks the pre-execution persistence contract. Retain evidence,
            # including when an old release changed attempting back to pending.
            legacy_keys = set()
            for path, rows in rows_by_path.items():
                for row in rows:
                    if ambiguous_unlinked_attempt(row):
                        blocked[path].add(row["queue_id"])
                        legacy_keys.add(row["session_key"])
            for key, entry in self._entries.items():
                link = entry.restart_inbox_link
                if key in running_keys:
                    continue
                if not link and key not in legacy_keys:
                    continue
                candidate = entry.to_dict()
                mode = "parked"
                try:
                    if not link:
                        link = {"protocol": 0, "mode": "parked", "reason": "legacy execution state unknown"}
                        raise ValueError("Legacy inbox execution state is unknown")
                    if link.get("db_path") not in paths or link.get("session_key") != key:
                        raise ValueError("Untrusted or mismatched restart inbox location")
                    row = linked_row(link)
                    if row["state"] in ("delivered", "abandoned"):
                        mode = "delivered"
                    elif _owner_alive(row["owner_pid"], row["owner_started_at"]):
                        raise ValueError("Restart claim is still owned by a live process")
                    elif entry.suspended or key in legacy_keys:
                        raise ValueError("Suspended or ambiguous legacy recovery")
                    else:
                        home = self._profile_home_for_key(key) or self._routing_home
                        if home is None or not (Path(home) / "state.db").is_file():
                            raise ValueError("Canonical transcript database is missing")
                        db = self._db_for_key(key)
                        if db is None or db.get_session(link["session_id"]) is None:
                            raise ValueError("Canonical input session is missing")
                        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
                        scope = set_hermes_home_override(Path(home))
                        try:
                            # A compression repoint can remove the old id from the routing index.
                            # Pin the owning profile even for the probe's unknown-id fallback.
                            ingested = self.has_input_owner(link["session_id"], link["input_owner"])
                        finally:
                            reset_hermes_home_override(scope)
                        mode = "continuation" if ingested else "replay"
                        if row["state"] == "handed_off" and not ingested:
                            raise ValueError("Previously ingested input no longer has proof")
                        if not transition_link(link, "handed_off" if ingested else "pending", recovery=True):
                            raise ValueError("Restart recovery claim changed")
                    candidate.update(active_turn_token=None, active_turn_started_at=None,
                                     resume_pending=mode == "continuation",
                                     resume_reason="restart_interrupted" if mode == "continuation" else None,
                                     resume_turn_token=link.get("turn_token") if mode == "continuation" else None,
                                     last_resume_marked_at=(entry.last_resume_marked_at or _now()).isoformat()
                                     if mode == "continuation" else None)
                except Exception as exc:
                    mode = "parked"
                    logger.warning("Parking restart recovery for %s: %s", key, exc)
                candidate["restart_inbox_link"] = None if mode == "delivered" else dict(link, mode=mode)
                if mode == "delivered":
                    candidate["restart_inbox_settled_at"] = _now().isoformat()
                self._save_entry(key, entry_data=candidate, lock_held=True, require_primary=True)
                restored = type(entry).from_dict(candidate)
                for name in ("active_turn_token", "active_turn_started_at", "resume_pending", "resume_reason",
                             "resume_turn_token", "last_resume_marked_at", "restart_inbox_link",
                             "restart_inbox_settled_at"):
                    setattr(entry, name, getattr(restored, name))
                for path, rows in rows_by_path.items():
                    for row in rows:
                        if row["session_key"] == key and not (
                            mode == "replay" and path == link.get("db_path")
                            and row["queue_id"] == link.get("queue_id")
                        ):
                            blocked[path].add(row["queue_id"])
        return blocked

    def recover_interrupted_turns(self, max_age_seconds: int = 60 * 60) -> int:
        """Promote crash-left turn markers into ``resume_pending`` (unclean startup only).
        Old/invalid markers are cleared without resuming; suspended sessions are never re-armed.
        Returns the number of newly promoted sessions."""
        now = _now()
        max_age = timedelta(seconds=max(0, max_age_seconds))
        promoted = 0

        def _promote(entry: SessionEntry) -> bool:
            nonlocal promoted
            if entry.restart_inbox_link:
                return False  # The linked protocol owns clean and unclean recovery alike.
            if not entry.active_turn_token:
                return False
            started_at = entry.active_turn_started_at
            try:
                marker_is_stale = started_at is None or (
                    max_age_seconds > 0 and now - started_at > max_age
                )
            except TypeError:
                # Mixed aware/naive timestamps: clear rather than risk an unsafe old resume.
                marker_is_stale = True
            if not marker_is_stale and not entry.suspended:
                if entry.resume_pending:
                    # A drain-timeout marker is more specific; keep its reason
                    # and freshness, but advance ownership to this interrupted
                    # turn so an older final delivery cannot clear recovery.
                    entry.resume_turn_token = entry.active_turn_token
                    if entry.last_resume_marked_at is None:
                        entry.last_resume_marked_at = now
                else:
                    entry.resume_pending = True
                    entry.resume_reason = "restart_interrupted"
                    entry.resume_turn_token = entry.active_turn_token
                    entry.last_resume_marked_at = now  # freshness starts at discovery
                    promoted += 1
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True

        self._update_all_entries_locked(_promote)
        return promoted

    def discard_active_turn_markers(self) -> int:
        """Clear orphan turn markers after a verified clean shutdown."""
        def _discard(entry: SessionEntry) -> bool:
            if entry.restart_inbox_link:
                return False
            if not entry.active_turn_token and entry.active_turn_started_at is None:
                return False
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True
        return self._update_all_entries_locked(_discard)

    def mark_resume_pending(self, session_key: str, reason: str = "restart_timeout") -> bool:
        """Mark a session resumable after a restart interruption (keeps the session_id/transcript,
        unlike ``suspend_session``). True if marked."""
        def _apply(entry: SessionEntry):
            if entry.suspended:  # never override an explicit ``suspended`` (hard forced-wipe)
                return False
            entry.resume_pending = True
            entry.resume_reason = reason
            entry.last_resume_marked_at = _now()
            entry.resume_turn_token = entry.active_turn_token
        return self._update_entry(session_key, _apply)

    def clear_resume_pending(self, session_key: str) -> bool:
        """Clear the resume-pending flag after a successful resumed turn; True if cleared."""
        def _apply(entry: SessionEntry):
            if not entry.resume_pending:
                return False
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
            entry.resume_turn_token = None
        return self._update_entry(session_key, _apply)

    def clear_resume_pending_for_obligation(
        self, session_key: str, turn_token: Optional[str], *, allow_legacy: bool = False,
    ) -> bool:
        """Clear recovery only for the final delivery of the interrupted turn.

        A mismatched obligation is still a successfully handled no-op.  Legacy
        rows without tokens may be discharged only by an explicitly legacy
        tokenless obligation.
        """
        def _apply(entry: SessionEntry):
            link = entry.restart_inbox_link
            if link and turn_token and link.get("turn_token") == turn_token:
                from gateway.restart_inbox import linked_row, transition_link
                row = linked_row(link)
                owns = (row["owner_pid"], row["owner_started_at"]) == (
                    link["owner_pid"], link["owner_started_at"],
                )
                if not transition_link(link, "delivered", recovery=not owns):
                    raise RuntimeError("Could not settle restart inbox final delivery")
                entry.restart_inbox_link = None
                entry.restart_inbox_settled_at = _now().isoformat()
                if entry.active_turn_token == turn_token:
                    entry.active_turn_token = None
                    entry.active_turn_started_at = None
                entry.resume_pending = False
                entry.resume_reason = None
                entry.last_resume_marked_at = None
                entry.resume_turn_token = None
                return True
            if not entry.resume_pending:
                return False
            expected = entry.resume_turn_token
            matches = bool(expected and turn_token and expected == turn_token)
            legacy_matches = bool(allow_legacy and not expected and not turn_token)
            if not matches and not legacy_matches:
                return False
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
            entry.resume_turn_token = None
        self._update_entry(session_key, _apply)
        # The contract reports successful handling for missing/mismatched rows;
        # callers use False for persistence failures, which _update_entry raises.
        return True

    def prune_old_entries(self, max_age_days: int) -> int:
        """Drop routing entries idle (by ``updated_at``) for more than max_age_days; suspended
        entries and entries with active background processes are kept. Only the key -> session_id
        mapping is dropped (the transcript stays). ``max_age_days <= 0`` disables. Returns count."""
        if max_age_days is None or max_age_days <= 0:
            return 0
        cutoff = _now() - timedelta(days=max_age_days)
        with self._lock:
            self._ensure_loaded_locked()
            removed_keys = [
                key for key, entry in list(self._entries.items())
                if not entry.suspended
                and not entry.restart_inbox_link
                # The callback is keyed by session_key, NOT session_id.
                and not self._has_active_processes_safe(entry.session_key, context="prune")
                and entry.updated_at < cutoff
            ]
            for key in removed_keys:
                self._entries.pop(key, None)
            if removed_keys:
                self._save()
        if removed_keys:
            logger.info("SessionStore pruned %d entries older than %d days",
                        len(removed_keys), max_age_days)
        return len(removed_keys)

    def suspend_recently_active(self, max_age_seconds: int = 120) -> int:
        """Mark recently-active unfinished sessions after an unexpected exit.

        A terminal assistant/stop tail without tool calls proves the model turn
        completed; delivery recovery owns that case, and rerunning it would
        duplicate user work.  A durable active-turn token overrides that
        heuristic because it proves execution was still owned at the crash.
        """
        cutoff = _now() - timedelta(seconds=max_age_seconds)

        def _mark(entry: SessionEntry) -> bool:
            if entry.restart_inbox_link:
                return False
            settled = _parse_iso(entry.restart_inbox_settled_at)
            if settled is not None and entry.updated_at <= settled:
                return False
            if entry.resume_pending or entry.suspended or entry.updated_at < cutoff:
                return False
            try:
                db = self._db_for_key(entry.session_key)
                get_messages = getattr(db, "get_messages", None)
                tail_session_id = entry.session_id
                find_child = getattr(db, "find_live_compression_child", None)
                for _ in range(16):
                    child = find_child(tail_session_id) if callable(find_child) else None
                    child_id = child.get("id") if isinstance(child, dict) else None
                    if not child_id:
                        break
                    tail_session_id = str(child_id)
                messages: Any = get_messages(tail_session_id) if callable(get_messages) else []
                latest = messages[-1] if messages else None
            except Exception:
                latest = None
            if (
                not entry.active_turn_token
                and isinstance(latest, dict)
                and latest.get("role") == "assistant"
                and str(latest.get("finish_reason") or "").casefold() == "stop"
                and not latest.get("tool_calls")
            ):
                return False
            entry.resume_pending = True
            entry.resume_reason = "restart_interrupted"
            entry.last_resume_marked_at = _now()
            entry.resume_turn_token = entry.active_turn_token
            return True
        return self._update_all_entries_locked(_mark)
