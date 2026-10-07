"""SessionStore explicit suspension, crash-recovery markers, pruning and shared clock/id helpers."""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from hermes_state_ids import new_session_id

if TYPE_CHECKING:
    from gateway.session import SessionEntry, SessionSource

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.session")


def _now() -> datetime:
    """Return the current local time."""
    return datetime.now()


def _new_session_id(now: datetime) -> str:
    return new_session_id(now, hex_len=8)


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

    def _is_session_ended_in_db(self, session_id: str, session_key: Optional[str] = None) -> bool:
        """True iff state.db says the session is gone: ended (non-null end_reason) or hard-deleted
        (no row in a readable owning DB). No DB or a DB error -> False (same failure mode as
        ``_prune_stale_sessions_locked``). Lets routing self-heal a session finalized or deleted
        while the gateway stays alive. Store resolved from the owning profile.

        Used by ``get_or_create_session`` to self-heal at routing time: ``_prune_stale_sessions_locked``
        only runs at startup, so a session ended in the DB while the gateway stays alive (any path that
        finalizes the row without clearing sessions.json) would otherwise be reused as a live routing key
        and silently swallow every subsequent message until the next restart (#54878 — the live-gateway
        variant of #52804/FM9). A hard delete is the same shape one step further: the row is GONE, not
        merely ended, and reusing the route makes run_agent's INSERT OR IGNORE resurrect the deleted
        session with its old id (#42422) — so a missing row is treated exactly like an ended one. DB
        errors are non-fatal — never block routing on a failed lookup.
        The store is resolved from the row's owning profile rather than the ambient scope: an unscoped
        background writer keeps its own copy of the same session, and comparing against that copy reports a
        live session as ended (#66887).

        Pass *session_key* when the owning key is known but the id may have left the routing index:
        after the self-heal re-homes the key, the id has no owner and would resolve to the launch
        store, which never holds a routed profile's rows (#118862).
        """
        db = self._db_for_key(session_key) if session_key else self._db_for_session_id(session_id)
        if not db or not session_id:
            return False
        try:
            row = db.get_session(session_id)
        except Exception:
            return False
        return row is None or row.get("end_reason") is not None

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

    def _update_all_entries_locked(self, mutate, *, exclude_session_keys=frozenset()) -> int:
        """Mutate eligible entries; never bulk-save over a concurrently owned lane."""
        with self._lock:
            self._ensure_loaded_locked()
            changed = [key for key, entry in self._entries.items()
                       if key not in exclude_session_keys and mutate(entry)]
            if exclude_session_keys:
                for key in changed:
                    self._save_entry(key, lock_held=True, allow_full_rewrite=False)
            elif changed:
                self._save()
        return len(changed)

    def suspend_session(self, session_key: str) -> bool:
        """Mark a session suspended so it auto-resets on next access (/stop). True if it existed.

        Used by ``/stop`` to prevent stuck sessions from being resumed after a gateway restart (#7536).
        """
        return self._update_entry(session_key, lambda e: setattr(e, "suspended", True))

    def _set_turn_marker_locked(self, session_key: str, entry: SessionEntry, token, started_at, *, human: bool = True) -> None:
        """Persist the active-turn pair BEFORE publishing it in memory, so a failed write can
        neither leak an unowned token nor drop a live one. Lock held."""
        candidate = entry.to_dict()
        candidate["active_turn_token"] = token
        candidate["active_turn_started_at"] = _iso(started_at)
        candidate["active_turn_human"] = bool(human)
        touched = _now() if started_at is not None else None
        if touched is not None:
            # Keeps the legacy 120s startup heuristic working for an older binary during a rolling
            # downgrade/upgrade window.
            candidate["updated_at"] = touched.isoformat()
        self._save_entry(session_key, entry_data=candidate, lock_held=True)
        entry.active_turn_token = token
        entry.active_turn_started_at = started_at
        entry.active_turn_human = bool(human)
        if touched is not None:
            entry.updated_at = touched

    def mark_turn_active(self, session_key: str, *, human: bool = True) -> Optional[str]:
        """Persist exact ownership of the running agent turn; returns the opaque token for
        :meth:`clear_turn_active`. Re-marking replaces the previous token so a stale asynchronous
        unwind cannot clear a newer turn."""
        token = uuid.uuid4().hex
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None:
                return None
            # Aware UTC, unlike the local wall clock elsewhere: the next process compares it with
            # epoch transcript timestamps and may run in another zone (DST, container vs unit TZ).
            self._set_turn_marker_locked(session_key, entry, token, datetime.now(timezone.utc), human=human)
        return token

    def clear_turn_active(self, session_key: str, token: str) -> bool:
        """Compare-and-swap clear an active-turn marker; ``False`` when the entry disappeared or a
        newer turn owns it."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or entry.active_turn_token != token:
                return False
            self._set_turn_marker_locked(session_key, entry, None, None)
        return True

    def recover_interrupted_turns(self, max_age_seconds: int = 60 * 60, *, exclude_session_keys=frozenset()) -> int:
        """Promote crash-left turn markers into ``resume_pending`` (unclean startup only).
        Old/invalid markers are cleared without resuming; suspended sessions are never re-armed.
        Returns the number of newly promoted sessions."""
        now, epoch_now = _now(), time.time()
        promoted = 0

        def _promote(entry: SessionEntry) -> bool:
            nonlocal promoted
            if not entry.active_turn_token:
                return False
            started_at = entry.active_turn_started_at
            # Epoch arithmetic: a pre-upgrade naive marker reads as local time, an aware one exactly.
            marker_is_stale = started_at is None or (
                max_age_seconds > 0 and epoch_now - started_at.timestamp() > max_age_seconds
            )
            if not marker_is_stale and not entry.suspended:
                if entry.resume_pending:
                    # A drain-timeout marker is more specific; keep it.
                    if entry.last_resume_marked_at is None:
                        entry.last_resume_marked_at = now
                else:
                    entry.resume_pending = True
                    entry.resume_reason = "restart_interrupted"
                    entry.resume_marker_token = uuid.uuid4().hex
                    entry.resume_turn_id = entry.active_turn_token
                    entry.resume_human = bool(entry.active_turn_human)
                    if entry.restart_note_message_id and not str(entry.restart_note_message_id).startswith(("pending:", "sent:")):
                        # Reuse the one visible note for the new marker; shutdown delivery will
                        # delete it before posting a replacement, avoiding two visible notes.
                        entry.restart_note_reconcile_attempts = 0
                    else:
                        entry.restart_note_message_id = None
                        entry.restart_note_marker_token = None
                        entry.restart_note_turn_id = None
                        entry.restart_note_marked_at = None
                        entry.restart_note_reconcile_attempts = 0
                    entry.last_resume_marked_at = now  # freshness starts at discovery
                    promoted += 1
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True

        self._update_all_entries_locked(_promote, exclude_session_keys=exclude_session_keys)
        return promoted

    def discard_active_turn_markers(self, *, exclude_session_keys=frozenset()) -> int:
        """Clear orphan turn markers after a verified clean shutdown."""
        def _discard(entry: SessionEntry) -> bool:
            if not entry.active_turn_token and entry.active_turn_started_at is None:
                return False
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True
        return self._update_all_entries_locked(_discard, exclude_session_keys=exclude_session_keys)

    def mark_resume_pending(
        self, session_key: str, reason: str = "restart_timeout", *,
        turn_id: Optional[str] = None, human: bool = True,
    ) -> bool:
        """Mark a session resumable after a restart interruption (keeps the session_id/transcript,
        unlike ``suspend_session``). A repeated shutdown pass for the same durable turn preserves
        its marker token and note id so it cannot post a duplicate note."""
        def _apply(entry: SessionEntry):
            if entry.suspended:  # never override an explicit ``suspended`` (hard forced-wipe)
                return False
            same_turn = bool(turn_id and entry.resume_pending and entry.resume_turn_id == turn_id)
            entry.resume_pending = True
            entry.resume_reason = reason
            entry.resume_human = bool(human)
            if not same_turn:
                entry.resume_marker_token = uuid.uuid4().hex
                entry.resume_turn_id = turn_id
                reconcile_claims = getattr(self, "_restart_note_reconcile_claims", None)
                if reconcile_claims is None:
                    reconcile_claims = self._restart_note_reconcile_claims = {}
                claimed_note = reconcile_claims.get(session_key)
                if (claimed_note and entry.restart_note_message_id
                        and claimed_note[3] == entry.restart_note_message_id):
                    # The old turn has already won ownership of the old visible note. Detach that
                    # pointer before publishing the successor marker so the old delivery can delete
                    # only its own note while the successor allocates a fresh one.
                    entry.restart_note_message_id = None
                    entry.restart_note_marker_token = None
                    entry.restart_note_turn_id = None
                    entry.restart_note_marked_at = None
                    entry.restart_note_reconcile_attempts = 0
                if entry.restart_note_message_id and not str(entry.restart_note_message_id).startswith(("pending:", "sent:")):
                    # A stale visible note is the single slot for this session. Shutdown delivery
                    # removes it before claiming a replacement note, preserving one-visible-note.
                    entry.restart_note_reconcile_attempts = 0
                else:
                    entry.restart_note_message_id = None
                    entry.restart_note_marker_token = None
                    entry.restart_note_turn_id = None
                    entry.restart_note_marked_at = None
                    entry.restart_note_reconcile_attempts = 0
                entry.last_resume_marked_at = _now()
        return self._update_entry(session_key, _apply)

    def get_resume_pending_marker(self, session_key: str) -> Optional[tuple]:
        """Snapshot the current marker before an interrupt can yield to a successor."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or not entry.resume_pending:
                return None
            return (entry.session_id, entry.resume_marker_token, entry.last_resume_marked_at)

    def claim_restart_note(
        self, session_key: str, *, expected_marker: Optional[tuple] = None,
        reclaim_pending: bool = False,
    ) -> bool:
        """Atomically reserve the current turn's note send before touching the network."""
        def _apply(entry: SessionEntry):
            if not entry.resume_pending:
                return False
            current = (entry.session_id, entry.resume_marker_token, entry.last_resume_marked_at)
            if expected_marker is not None and expected_marker != current:
                return False
            existing = entry.restart_note_message_id
            if existing and not (reclaim_pending and str(existing).startswith("pending:")):
                return False
            entry.restart_note_message_id = f"pending:{entry.resume_marker_token or uuid.uuid4().hex}"
            entry.restart_note_marker_token = entry.resume_marker_token
            entry.restart_note_turn_id = entry.resume_turn_id
            entry.restart_note_marked_at = entry.last_resume_marked_at
            entry.restart_note_reconcile_attempts = 0
            return True
        return self._update_entry(session_key, _apply)

    def release_restart_note_claim(self, session_key: str, *, expected_marker: Optional[tuple] = None) -> bool:
        """Release a pre-send note reservation when no message was accepted by the adapter."""
        def _apply(entry: SessionEntry):
            claim = entry.restart_note_message_id
            if not claim or not str(claim).startswith("pending:"):
                return False
            current = (entry.session_id, entry.resume_marker_token, entry.last_resume_marked_at)
            if expected_marker is not None and expected_marker != current:
                return False
            entry.restart_note_message_id = None
            entry.restart_note_marker_token = None
            entry.restart_note_turn_id = None
            entry.restart_note_marked_at = None
            entry.restart_note_reconcile_attempts = 0
            return True
        return self._update_entry(session_key, _apply)

    def set_restart_note_message_id(
        self, session_key: str, message_id: str, *, expected_marker: Optional[tuple] = None,
    ) -> bool:
        """Persist the visible interruption note id exactly once for the current resume marker."""
        def _apply(entry: SessionEntry):
            if not entry.resume_pending:
                return False
            existing = entry.restart_note_message_id
            if existing and not str(existing).startswith("pending:"):
                return False
            if expected_marker is not None and expected_marker != (
                entry.session_id, entry.resume_marker_token, entry.last_resume_marked_at,
            ):
                return False
            entry.restart_note_message_id = str(message_id)
            entry.restart_note_marker_token = entry.resume_marker_token
            entry.restart_note_turn_id = entry.resume_turn_id
            entry.restart_note_marked_at = entry.last_resume_marked_at
            entry.restart_note_reconcile_attempts = 0
            return True
        return self._update_entry(session_key, _apply)

    def get_restart_note(self, session_key: str) -> Optional[tuple]:
        """Return ``(session_id, marker_token, marked_at, message_id)`` for note reconciliation."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or (not entry.resume_pending and not entry.restart_note_message_id):
                return None
            return (
                entry.session_id,
                entry.restart_note_marker_token or entry.resume_marker_token,
                entry.restart_note_marked_at or entry.last_resume_marked_at,
                entry.restart_note_message_id,
            )

    def claim_restart_note_reconciliation(
        self, session_key: str, *, expected_marker: Optional[tuple] = None,
        expected_note: Optional[tuple] = None,
    ) -> bool:
        """CAS-claim a visible note before awaiting its network deletion.

        A successor re-mark notices this claim and detaches the old pointer before publishing its
        marker, so an old answer can never delete a successor-owned note.
        """
        with self._lock:
            entry = self._entry_locked(session_key)
            note_id = getattr(entry, "restart_note_message_id", None) if entry is not None else None
            if entry is None or not note_id or str(note_id).startswith(("pending:", "sent:")):
                return False
            note = (
                entry.session_id,
                entry.restart_note_marker_token or entry.resume_marker_token,
                entry.restart_note_marked_at or entry.last_resume_marked_at,
                note_id,
            )
            if expected_note is not None and tuple(expected_note[:4]) != note:
                return False
            owner_marker = note[:3]
            if expected_marker is not None:
                if entry.resume_pending:
                    if expected_marker != (
                        entry.session_id, entry.resume_marker_token, entry.last_resume_marked_at,
                    ):
                        return False
                elif expected_marker != owner_marker:
                    return False
            claims = getattr(self, "_restart_note_reconcile_claims", None)
            if claims is None:
                claims = self._restart_note_reconcile_claims = {}
            current_claim = claims.get(session_key)
            if current_claim is not None and current_claim != note:
                return False
            claims[session_key] = note
            return True

    def release_restart_note_reconciliation(
        self, session_key: str, *, expected_note: Optional[tuple] = None,
    ) -> bool:
        """Release a pre-delete note claim without changing the durable pointer."""
        claims = getattr(self, "_restart_note_reconcile_claims", None)
        if not claims:
            return False
        with self._lock:
            current = claims.get(session_key)
            if current is None or (expected_note is not None and tuple(expected_note[:4]) != current):
                return False
            claims.pop(session_key, None)
            return True

    def clear_restart_note(
        self, session_key: str, *, expected_marker: Optional[tuple] = None,
        expected_note: Optional[tuple] = None,
    ) -> bool:
        """Clear the note id after its resumed answer was deleted or replaced."""
        def _apply(entry: SessionEntry):
            if not entry.restart_note_message_id:
                return False
            current = (entry.session_id, entry.resume_marker_token, entry.last_resume_marked_at)
            if expected_marker is not None:
                if entry.resume_pending:
                    if expected_marker != current:
                        return False
                elif expected_marker != (
                    entry.session_id,
                    entry.restart_note_marker_token or entry.resume_marker_token,
                    entry.restart_note_marked_at or entry.last_resume_marked_at,
                ):
                    # The owning turn may have cleared resume_pending before final delivery. In that
                    # case the note's captured marker is the only remaining ownership evidence.
                    return False
            if expected_note is not None and expected_note != (
                entry.session_id,
                entry.restart_note_marker_token or entry.resume_marker_token,
                entry.restart_note_marked_at or entry.last_resume_marked_at,
                entry.restart_note_message_id,
            ):
                return False
            entry.restart_note_message_id = None
            entry.restart_note_marker_token = None
            entry.restart_note_turn_id = None
            entry.restart_note_marked_at = None
            entry.restart_note_reconcile_attempts = 0
        return self._update_entry(session_key, _apply)

    def record_restart_note_reconcile_failure(self, session_key: str, *, max_attempts: int = 3) -> bool:
        """Count a failed edit/delete attempt; drop a permanently unreachable note after a bound."""
        def _apply(entry: SessionEntry):
            if not entry.restart_note_message_id or str(entry.restart_note_message_id).startswith(("pending:", "sent:")):
                return False
            entry.restart_note_reconcile_attempts += 1
            if entry.restart_note_reconcile_attempts < max_attempts:
                return False
            entry.restart_note_message_id = None
            entry.restart_note_marker_token = None
            entry.restart_note_turn_id = None
            entry.restart_note_marked_at = None
            entry.restart_note_reconcile_attempts = 0
            return True
        return self._update_entry(session_key, _apply)

    def clear_resume_pending(
        self, session_key: str, *, expected_marker: Optional[tuple] = None,
        expected_turn_id: Optional[str] = None,
    ) -> bool:
        """Clear the resume-pending flag after a successful resumed turn; True if cleared.

        A shutdown drain may re-mark the same turn with a fresh marker after the turn
        started. ``expected_turn_id`` permits that owner to clear its replacement
        marker while still refusing a marker belonging to a later turn.
        """
        def _apply(entry: SessionEntry):
            if not entry.resume_pending:
                return False
            current = (entry.session_id, entry.resume_marker_token, entry.last_resume_marked_at)
            if expected_marker is not None and expected_marker != current:
                if expected_turn_id is None or entry.resume_turn_id != expected_turn_id:
                    return False
            entry.resume_pending = False
            entry.resume_reason = None
            entry.resume_marker_token = None
            entry.resume_turn_id = None
            entry.resume_human = True
            entry.last_resume_marked_at = None
        return self._update_entry(session_key, _apply)


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
