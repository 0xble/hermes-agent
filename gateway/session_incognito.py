"""RAM-only gateway history and content-free, fail-safe privacy routing metadata."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path

from agent.incognito import is_incognito, register_session, set_private_route
from utils import atomic_json_write


class SessionIncognitoMixin:
    def _incognito_path(self) -> Path:
        return self.sessions_dir / "incognito.json"

    def _load_incognito_locked(self) -> None:
        path = self._incognito_path()
        records = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(records, dict):
            raise ValueError("Invalid incognito routing metadata, refusing ingress")
        self._incognito_records = records
        stale = set()
        for key, record in records.items():
            entry = self._entries.get(key)
            recorded = record.get("session_id") if isinstance(record, dict) else None
            if entry is None or entry.session_id != recorded:
                # A stale boundary must not silently make this topic public. Refuse only
                # this topic, keep its recorded identity private, and load every other one.
                stale.add(key)
                if isinstance(recorded, str) and recorded:
                    register_session(recorded, True)
                logging.getLogger(__name__).warning(
                    "Incognito boundary does not match the routing index; refusing that topic until "
                    "its record is removed from %s", path.name)
                continue
            entry._incognito_persisted = entry.to_dict()
            entry.incognito = True
            if "active_override" in record:
                entry.model_override = deepcopy(record["active_override"])
                entry.model_override_scope = "session"
                entry.model_role = None
            # Without a configured private route, retain the saved pick's scope
            # and absolute deadline. A restart must not turn a timed pick permanent.
            register_session(entry.session_id, True)
            register_session(key, True)
            set_private_route((entry.session_id, key), record.get("active_override"))
        self._incognito_stale = frozenset(stale)

    def _refuse_stale_incognito(self, session_key: str) -> None:
        if session_key in getattr(self, "_incognito_stale", ()):
            raise ValueError("Incognito boundary does not match the routing index")

    def incognito_enabled(self, session_key: str) -> bool:
        with self._lock:
            self._ensure_loaded_locked()
            self._refuse_stale_incognito(session_key)
            return session_key in getattr(self, "_incognito_records", {})

    def published_incognito_enabled(self, session_key: str) -> bool | None:
        """Lock-free read for the event loop; None until the routing index has loaded.

        The index is replaced, never mutated, under the store lock, so one attribute read
        observes a complete published state, exactly as a locked read would.
        """
        if not getattr(self, "_loaded", False) or session_key in getattr(self, "_incognito_stale", ()):
            return None
        records = getattr(self, "_incognito_records", None)
        return None if records is None else session_key in records

    def incognito_metadata(self, session_key: str) -> dict:
        with self._lock:
            self._ensure_loaded_locked()
            return deepcopy(getattr(self, "_incognito_records", {}).get(session_key, {}))

    def set_incognito(self, session_key: str, enabled: bool, *, active_override=None):
        """Persist the privacy decision before admitting content. No history is serialized."""
        from gateway.session import SessionEntry, sanitize_model_override
        entry = self.lookup_by_session_key(session_key)
        if entry is None:
            raise ValueError("No session for incognito boundary")
        if bool(entry.incognito) == bool(enabled):
            return entry
        # Read the saved prefix before registering the private identity. Reads stay available
        # afterward too, but the normal replay path may repair malformed role alternation.
        history = self.load_transcript(entry.session_id) if enabled else None
        if enabled:
            self._flush_public_token_counts()
        with self._lock:
            records = deepcopy(getattr(self, "_incognito_records", {}))
            if enabled:
                record = {
                    "session_id": entry.session_id,
                    "model_override": sanitize_model_override(entry.model_override),
                    "model_override_scope": entry.model_override_scope,
                    "model_role": entry.model_role,
                }
                if active_override is not None:
                    record["active_override"] = sanitize_model_override(active_override)
                records[session_key] = record
            else:
                records.pop(session_key, None)
            atomic_json_write(self._incognito_path(), records)
            self._incognito_records = records
            # The rewritten record matches this topic's route again.
            self._incognito_stale = getattr(self, "_incognito_stale", frozenset()) - {session_key}
            if enabled:
                entry._incognito_persisted = entry.to_dict()
                self._lazy("_incognito_histories", dict)[entry.session_id] = deepcopy(history)
                entry.incognito = True
                if active_override is not None:
                    entry.model_override = sanitize_model_override(active_override)
                    entry.model_override_scope = "session"
                    entry.model_role = None
                register_session(entry.session_id, True)
                register_session(session_key, True)
            else:
                saved = SessionEntry.from_dict(entry._incognito_persisted)
                entry.__dict__.clear()
                entry.__dict__.update(saved.__dict__)
                self._lazy("_incognito_histories", dict).pop(entry.session_id, None)
                register_session(entry.session_id, False)
                register_session(session_key, False)
                from gateway.incognito import discard_private_topic
                discard_private_topic(session_key)
        return entry

    def _flush_public_token_counts(self) -> None:
        """Apply usage deltas queued while public before the session becomes private."""
        try:
            db = self._db
            flush = getattr(db, "flush_token_counts", None)
            if callable(flush):
                from agent.incognito import shared_worker_context
                shared_worker_context().run(flush)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Incognito: public usage flush incomplete")

    def set_incognito_model(self, session_key: str, override):
        """Record only the configured route, never a user-entered private /model selection."""
        from gateway.session import sanitize_model_override
        cleaned = sanitize_model_override(override)
        with self._lock:
            self._ensure_loaded_locked()
            records = deepcopy(self._incognito_records)
            records[session_key]["active_override"] = cleaned
            atomic_json_write(self._incognito_path(), records)
            self._incognito_records = records
            entry = self._entries[session_key]
            entry.model_override = cleaned
            entry.model_override_scope = "session"
            entry.model_role = None
            # Auxiliary calls for this private interval stay on the same configured route.
            set_private_route((entry.session_id, session_key), cleaned)

    def _private_history(self, session_id: str):
        """None means public. An empty list is a valid private conversation."""
        if not is_incognito(session_id):
            return None
        histories = self._lazy("_incognito_histories", dict)
        if session_id not in histories:
            # Restart loses the RAM tail, not the durable prefix.
            histories[session_id] = self._load_saved_transcript(session_id)
        return histories[session_id]
