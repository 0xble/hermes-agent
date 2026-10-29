"""Persisted source recovery for synthetic process events, including legacy handoffs."""

import dataclasses
import logging
import threading
from typing import Optional

from gateway.config import Platform, _BUILTIN_PLATFORM_VALUES
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewayProcessEventSourcesMixin:
    def _build_process_event_source(self, evt: dict):
        """Resolve the canonical source for a synthetic background-process event.

        Prefer the persisted session-store origin; the active foreground event causes cross-topic bleed.
        """
        from gateway.run import _parse_session_key
        from gateway.run_notifications import _raw_process_event_session_id
        session_key = str(evt.get("session_key") or "").strip()
        event_profile = str(evt.get("profile") or "").strip() or None

        def _stamp_profile(source):
            from gateway.session_identity import identity_of
            if identity_of(source) is not None:
                return source  # The restored receiving-bot/runtime identity is authoritative.
            if source is None or not event_profile or getattr(source, "profile", None) == event_profile:
                return source
            try:
                return dataclasses.replace(source, profile=event_profile)
            except Exception:
                return source

        derived = {}
        if session_key:
            try:
                self.session_store._ensure_loaded()
                entry = (self.session_store.lookup_by_session_key(session_key)
                         or self.session_store.lookup_by_session_id(session_key))
                if entry and getattr(entry, "origin", None):
                    return self._restored_source(entry)
            except Exception as exc:
                logger.debug("Synthetic process-event session-store lookup failed for %s: %s", session_key, exc)
            cached_source = self._get_cached_session_source(session_key)
            if cached_source is not None:
                return _stamp_profile(cached_source)
            derived = _parse_session_key(session_key) or {}
        profile = event_profile or derived.get("profile")
        platform_name = str(evt.get("platform") or derived.get("platform") or "").strip().lower()
        chat_type = str(evt.get("chat_type") or derived.get("chat_type") or "").strip().lower()
        chat_id = str(evt.get("chat_id") or derived.get("chat_id") or "").strip()
        if platform_name and chat_id and not chat_type:
            chat_type = self._stored_process_chat_type(platform_name, chat_id, profile)
        if not platform_name or not chat_type or not chat_id:
            # Raw API keys legitimately have no messaging source. Resolve persisted
            # origins first, then leave this recognized route to the API dispatcher.
            if _raw_process_event_session_id(evt):
                return None
            log = self._unresolvable_process_source_log(evt)
            log(
                "Synthetic event source unresolvable: "
                "session_key=%r platform=%r chat_type=%r chat_id=%r evt_type=%s",
                session_key, platform_name, chat_type, chat_id, evt.get("type", "?"),
            )
            return None
        try:
            platform = Platform(platform_name)
            # Reject dynamic pseudo-members: plugin platforms must be registered.
            if platform.value not in _BUILTIN_PLATFORM_VALUES:
                try:
                    from gateway.platform_registry import platform_registry
                    if not platform_registry.is_registered(platform.value):
                        raise ValueError(platform_name)
                except Exception:
                    raise ValueError(platform_name)
        except Exception:
            logger.warning("Synthetic process event has invalid platform metadata: %r", platform_name)
            return None

        def _opt(field: str) -> Optional[str]:
            return str(evt.get(field) or "").strip() or None

        scope_id = _opt("scope_id")
        if scope_id is None and chat_type not in ("dm", "thread"):
            # Reconstructed scoped-chat source without scope_id: a relay connector's tenant guard may
            # decline the reply. Warn, don't fail (native adapters need no scope_id).
            logger.warning(
                "Synthetic event source for %s chat=%s (%s) reconstructed "
                "without scope_id; scoped relay egress may be declined by "
                "the connector's tenant guard (user_id fallback only).", platform_name, chat_id, chat_type,
            )
        return SessionSource(
            platform=platform, chat_id=chat_id, chat_type=chat_type, thread_id=_opt("thread_id"),
            user_id=_opt("user_id"), user_name=_opt("user_name"), scope_id=scope_id, profile=profile,
        )

    def _stored_process_chat_type(self, platform: str, chat_id: str, profile) -> str:
        """Recover only a chat's known type, never a sibling topic's thread or identity."""
        try:
            types = {
                entry.origin.chat_type for entry in self.session_store.list_sessions()
                if entry.origin is not None and entry.origin.platform.value == platform
                and str(entry.origin.chat_id) == chat_id
                and (entry.origin.profile or "default") == (profile or "default")
                and entry.origin.chat_type
            }
        except Exception:
            logger.debug("Synthetic process-event chat-type lookup failed", exc_info=True)
            return ""
        return types.pop() if len(types) == 1 else ""

    def _unresolvable_process_source_log(self, evt: dict):
        """Watcher ticks and newly built completion events share one warning per process."""
        if evt.get("type") in {"async_delegation", "delegation_auto_resume"}:
            return logger.debug
        identity = evt.get("session_id") or (evt.get("profile"), evt.get("session_key"))
        lock = self.__dict__.setdefault("_unresolvable_process_source_lock", threading.Lock())
        with lock:
            seen = self.__dict__.setdefault("_unresolvable_process_sources", set())
            repeated = identity in seen
            seen.add(identity)
        return logger.debug if repeated else logger.warning
