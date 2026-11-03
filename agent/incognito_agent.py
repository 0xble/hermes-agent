"""Agent-owned volatile state for the incognito interval."""
from copy import deepcopy
from functools import wraps

from agent.incognito import incognito_scope, is_incognito, register_session


def private_turn(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with incognito_scope(getattr(self, "incognito", False), getattr(self, "session_id", None)):
            return method(self, *args, **kwargs)
    return wrapped


def private_init(method):
    @wraps(method)
    def wrapped(agent, *args, **kwargs):
        private = bool(kwargs.get("incognito", False)) or is_incognito(kwargs.get("session_id"))
        if private:
            kwargs["incognito"] = True
        with incognito_scope(private, kwargs.get("session_id")):
            result = method(agent, *args, **kwargs)
            if private:
                agent._incognito_snapshot = _snapshot(agent)
                agent._incognito_snapshot["_session_messages"] = []
                agent._incognito_settings = {
                    "save_trajectories": kwargs.get("save_trajectories", False),
                    "skip_background_review": kwargs.get("skip_background_review", False),
                    "_persist_disabled": False,
                    "compression_checkpoint_required": False,
                }
                agent._incognito_checkpoint_enabled = kwargs.get("checkpoints_enabled", False)
                # Leaving restores the built-in compressor's store. A configured external
                # engine was never built for this agent, so owners should rebuild it.
                agent._incognito_engine_db = getattr(agent, "_incognito_session_db", None)
                agent._incognito_born = True
            return result
    return wrapped


def private_delegate(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        parent = kwargs.get("parent_agent")
        private = getattr(parent, "incognito", False) is True
        with incognito_scope(private, getattr(parent, "session_id", None)):
            return method(*args, **kwargs)
    return wrapped


def _snapshot(agent):
    names = ("_session_messages", "_cached_system_prompt", "_last_flushed_db_idx",
             "_flushed_db_message_ids", "_db_flush_scan_prefix", "_session_db_created",
             "_session_row_replay_pending", "session_id")
    return {name: deepcopy(getattr(agent, name)) for name in names if hasattr(agent, name)}


class IncognitoMixin:
    def flush_public_memory(self) -> None:
        """Deliver memory writes queued while public, before a privacy boundary is recorded."""
        manager = getattr(self, "_memory_manager", None)
        flush = getattr(manager, "flush_public_writes", None)
        if callable(flush):
            try:
                flush()
            except Exception:
                import logging
                logging.getLogger(__name__).warning("Incognito: public memory flush incomplete")

    def set_incognito(self, enabled: bool = True) -> None:
        """Cut memory work and detach persistence. Turning off drops private history.

        The gateway owns model restoration and its own boundary snapshot. This
        method also protects non-gateway callers and existing agent references.
        """
        enabled = bool(enabled)
        if enabled == bool(getattr(self, "incognito", False)):
            return
        if enabled:
            self.flush_public_memory()
            self._incognito_snapshot = _snapshot(self)
            self._incognito_settings = {
                name: getattr(self, name, None) for name in
                ("save_trajectories", "skip_background_review", "compression_checkpoint_required", "_persist_disabled")
            }
            self._incognito_session_db = getattr(self, "_session_db", None)
            checkpoint = getattr(self, "_checkpoint_mgr", None)
            self._incognito_checkpoint_enabled = getattr(checkpoint, "enabled", False)
            engine = getattr(self, "context_compressor", None)
            self._incognito_engine_db = getattr(engine, "_session_db", None)
            # A third-party context engine can own opaque persistent stores. Use
            # RAM compaction rather than dispatching content to one while private.
            self._incognito_context_engine = engine
            from agent.context_compressor import ContextCompressor
            if engine is not None and not isinstance(engine, ContextCompressor):
                self.context_compressor = ContextCompressor(
                    model=getattr(self, "model", ""), quiet_mode=True,
                    base_url=getattr(self, "base_url", ""), api_key=getattr(self, "api_key", ""),
                    provider=getattr(self, "provider", ""), api_mode=getattr(self, "api_mode", "chat_completions"),
                )
            if engine is not None and hasattr(engine, "_session_db"):
                engine._session_db = None
            self.incognito = True
            register_session(getattr(self, "session_id", ""), True)
            self._session_db = None
            self._persist_disabled = True
            self.save_trajectories = False
            self.skip_background_review = True
            self.compression_checkpoint_required = False
            if checkpoint is not None:
                checkpoint.enabled = False
            from agent.background_review import cancel_background_review_for_live_turn
            try:
                cancel_background_review_for_live_turn(self)
            except Exception:
                pass
        manager = getattr(self, "_memory_manager", None)
        if manager is not None:
            try:
                manager.set_read_only(enabled)
            except Exception:
                # Refuse writes even if a provider's optional transition hook
                # fails. Never print the exception, which can contain content.
                manager.read_only = enabled
                import logging
                logging.getLogger(__name__).warning("Incognito: memory transition incomplete")
        store = getattr(self, "_memory_store", None)
        if store is not None:
            store.read_only = enabled
        if not enabled:
            for name, value in getattr(self, "_incognito_snapshot", {}).items():
                setattr(self, name, value)
            self._session_db = getattr(self, "_incognito_session_db", None)
            settings = getattr(self, "_incognito_settings", None) or {
                "_persist_disabled": False, "save_trajectories": False,
                "skip_background_review": False, "compression_checkpoint_required": False,
            }
            for name, value in settings.items():
                setattr(self, name, value)
            checkpoint = getattr(self, "_checkpoint_mgr", None)
            if checkpoint is not None:
                checkpoint.enabled = getattr(self, "_incognito_checkpoint_enabled", False)
            engine = getattr(self, "_incognito_context_engine", getattr(self, "context_compressor", None))
            self.context_compressor = engine
            if engine is not None and hasattr(engine, "_session_db"):
                engine._session_db = getattr(self, "_incognito_engine_db", None)
            self.incognito = False
            register_session(getattr(self, "session_id", ""), False)
            self._incognito_snapshot = None
            self._incognito_session_db = None
            # The snapshot restored the frozen prompt, and read-only memory
            # never changed. Reloading either here would invalidate its prefix.
