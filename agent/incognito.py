"""Process-local privacy boundary shared by turns, tools and background workers.

Only routing metadata belongs in durable session stores. This registry deliberately
holds no content and is never serialized.
"""
from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
import logging
import threading
from weakref import WeakValueDictionary


class _SessionWork:
    """Revocable provenance, retained only by scopes and their copied workers."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.revoked = False
        self.state: dict = {}

_scope: ContextVar[bool] = ContextVar("hermes_incognito", default=False)
_volatile: ContextVar[dict | None] = ContextVar("hermes_incognito_state", default=None)
_session_id: ContextVar[str | None] = ContextVar("hermes_incognito_session", default=None)
_work: ContextVar[tuple[_SessionWork, ...]] = ContextVar("hermes_incognito_work", default=())
_public_work: WeakValueDictionary[str, _SessionWork] = WeakValueDictionary()
_sessions: set[str] = set()
_private_states: dict[str, dict] = {}
_lock = threading.RLock()


def is_incognito(session_id: str | None = None) -> bool:
    if _scope.get():
        return True
    if session_id is None:
        session_id = _session_id.get()
    with _lock:
        return any(work.revoked for work in _work.get()) or (
            isinstance(session_id, str) and session_id in _sessions
        )


def any_incognito_sessions() -> bool:
    """Protect unattributable ingress without guessing which private topic owns it."""
    with _lock:
        return bool(_sessions)


def register_session(session_id: str, enabled: bool = True) -> None:
    with _lock:
        if enabled:
            # Revoke at entry, even if a worker never runs during the interval.
            work = _public_work.pop(session_id, None)
            if work is not None:
                work.revoked = True
            _sessions.add(session_id)
        else:
            _sessions.discard(session_id)
            _private_states.pop(session_id, None)
    install_logging_filter()


def _private_work_state(session_id: str | None) -> dict:
    # Called under _lock. Obsolete workers must not recreate a departed session
    # or share the scratch state belonging to a later private interval.
    for work in _work.get():
        if work.revoked:
            return work.state
    return _private_states.setdefault(session_id, {}) if isinstance(session_id, str) and session_id in _sessions else {}


@contextmanager
def incognito_scope(enabled: bool = True, session_id: str | None = None):
    # A nested off scope must not weaken an inherited private scope.
    session_id = session_id if session_id is not None else _session_id.get()
    with _lock:
        enabled = bool(enabled) or is_incognito() or is_incognito(session_id)
        work = _work.get()
        if not enabled and session_id and not any(item.session_id == session_id for item in work):
            current = _public_work.get(session_id)
            if current is None:
                current = _SessionWork(session_id)
                _public_work[session_id] = current
            # Keep named parent provenance when binding a nested child session.
            work = (*work, current)
        state = _volatile.get() if is_incognito() else None
        if enabled and state is None:
            state = _private_work_state(session_id)
    token = _scope.set(enabled)
    state_token = _volatile.set(state)
    session_token = _session_id.set(session_id)
    work_token = _work.set(work)
    install_logging_filter()
    try:
        yield
    finally:
        _work.reset(work_token)
        _session_id.reset(session_token)
        _volatile.reset(state_token)
        _scope.reset(token)


def shared_worker_context() -> Context:
    """Neutralize turn privacy for shared workers, preserving profile and secret context.

    Only shared dispatchers use this boundary. Nested turn scopes still cannot
    weaken inherited privacy; each dispatched event establishes its own scope.
    """
    context = copy_context()
    context.run(_scope.set, False)
    context.run(_volatile.set, None)
    context.run(_session_id.set, None)
    context.run(_work.set, ())
    return context


def private_state(namespace: str) -> dict | None:
    """Return private RAM state, isolated from public caches and discarded on exit."""
    with _lock:
        if not is_incognito():
            return None
        state = _volatile.get()
        if state is None:
            state = _private_work_state(_session_id.get())
            _volatile.set(state)
        return state.setdefault(namespace, {})


class _PrivacyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        private = bool(getattr(record, "_hermes_incognito", False)) or is_incognito(
            getattr(record, "session_id", None))
        record._hermes_incognito = private
        if private:
            record.msg = "Incognito: diagnostic content suppressed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_filter = _PrivacyFilter()
_installed_factory = None


def install_logging_filter() -> None:
    """Redact before *any* handler, including ones installed after startup."""
    global _installed_factory
    with _lock:
        factory = logging.getLogRecordFactory()
        if factory is _installed_factory:
            return

        def private_record(*args, **kwargs):
            record = factory(*args, **kwargs)
            _filter.filter(record)
            return record

        logging.setLogRecordFactory(private_record)
        _installed_factory = private_record


def tool_refusal(name: str, args: dict) -> str | None:
    """Refuse implicit durable Hermes state, not user-requested file outputs."""
    if not is_incognito():
        return None
    lower = name.lower()
    hindsight_write = "hindsight" in lower and not lower.endswith(("recall", "reflect"))
    durable = name in {"skill_manage", "goal_set", "loop_set", "cronjob", "cron_create", "request_update", "request_restart"}
    memory_write = name == "memory" and args.get("action") not in {"read", "list", "view"}
    process = name in {"terminal", "process", "process_manage"} and (
        args.get("persist_on_release") or args.get("survive_restart"))
    if hindsight_write or durable or memory_write or process:
        return "This durable operation is unavailable in incognito mode."
    return None
