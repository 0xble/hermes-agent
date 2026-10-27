"""RAM-only gateway history and content-free, fail-safe privacy routing metadata."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from agent.incognito import is_incognito, register_session
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
        for key, record in records.items():
            entry = self._entries.get(key)
            if entry is None or entry.session_id != record["session_id"]:
                # A stale boundary must not silently make this topic public.
                raise ValueError("Incognito boundary does not match the routing index")
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

    def incognito_enabled(self, session_key: str) -> bool:
        with self._lock:
            self._ensure_loaded_locked()
            return session_key in getattr(self, "_incognito_records", {})

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

    def _private_history(self, session_id: str):
        """None means public. An empty list is a valid private conversation."""
        if not is_incognito(session_id):
            return None
        histories = self._lazy("_incognito_histories", dict)
        if session_id not in histories:
            # Restart loses the RAM tail, not the durable prefix.
            histories[session_id] = self._load_saved_transcript(session_id)
        return histories[session_id]
