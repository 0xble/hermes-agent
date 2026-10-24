"""Durable attended holds share the session routing entry, not a model override."""
from __future__ import annotations

from dataclasses import replace


def session_attended_pinned(store, session_key: str) -> bool:
    getter = getattr(store, "get_attended_pinned", None)
    return getter(session_key) is True if callable(getter) else False


class SessionAttendedMixin:
    def get_attended_pinned(self, session_key: str) -> bool:
        with self._lock:
            entry = self._entry_locked(session_key)
            return entry.attended_pinned if entry else False

    def set_attended_pinned(self, session_key: str, pinned: bool, *, expected_session_id: str) -> bool:
        """Publish only after persistence, fenced against a concurrent conversation reset."""
        from gateway.session_lifecycle import _now

        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or entry.session_id != expected_session_id:
                return False
            stamp = _now()
            candidate = replace(entry, attended_pinned=pinned, updated_at=stamp)
            if not pinned:
                candidate.last_user_activity_at = stamp
            self._save_entry(session_key, entry_data=candidate.to_dict(), lock_held=True)
            entry.attended_pinned = pinned
            entry.last_user_activity_at = candidate.last_user_activity_at
            entry.updated_at = stamp
            return True
