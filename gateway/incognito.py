"""Ingress scoping and RAM-only equivalents for gateway durability stores."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import sqlite3
import threading
from typing import Callable

from agent.incognito import any_incognito_sessions, incognito_scope, is_incognito, register_session

_TOPIC: ContextVar[str | None] = ContextVar("gateway_private_topic", default=None)


def private_topic() -> str | None:
    return _TOPIC.get()


_TRANSITIONING: set[str] = set()


def transition_pending(session_key: str | None) -> bool:
    return session_key in _TRANSITIONING


@contextmanager
def transition_scope(session_key: str):
    _TRANSITIONING.add(session_key)
    token = _TOPIC.set(session_key)
    try:
        with incognito_scope(True, session_key):
            yield
    finally:
        _TRANSITIONING.discard(session_key)
        _TOPIC.reset(token)


_RAM_LOCK = threading.RLock()
_RAM_KEEPERS: dict[str, sqlite3.Connection] = {}
_RAM_TOPICS: dict[str, set[str]] = {}


def discard_private_topic(session_key: str) -> None:
    """Drop private retry/results caches with the conversation, without touching other topics."""
    from gateway import outbox
    with outbox._STORES_LOCK:
        suffix = ":incognito:" + session_key
        for key in list(outbox._STORES):
            if key.endswith(suffix):
                outbox._STORES.pop(key, None)
    with _RAM_LOCK:
        for uri in _RAM_TOPICS.pop(session_key, set()):
            keeper = _RAM_KEEPERS.pop(uri, None)
            if keeper is not None:
                keeper.close()


def ram_connect(namespace: str, initialize=None, *, topic: str | None = None):
    """Keep each private SQLite store alive only in this process. Never use a disk journal."""
    import hashlib
    topic = topic or private_topic()
    namespace += ":topic:" + (topic or "unbound")
    uri = "file:incognito_" + hashlib.sha256(namespace.encode()).hexdigest() + "?mode=memory&cache=shared"
    with _RAM_LOCK:
        if uri not in _RAM_KEEPERS:
            keeper = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
            if initialize:
                initialize(keeper)
            _RAM_KEEPERS[uri] = keeper
            if topic is not None:
                _RAM_TOPICS.setdefault(topic, set()).add(uri)
    return sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=10)


def _routing_records() -> dict:
    """Privacy routing belongs to the launch home, even for multiplexed profiles."""
    from hermes_constants import get_hermes_home
    from pathlib import Path
    import os
    import sys

    launch_home = getattr(sys.modules.get("gateway.run"), "_hermes_home", None)
    path = Path(os.environ.get("HERMES_HOME") or launch_home or get_hermes_home()) / "sessions" / "incognito.json"
    if not path.exists():
        return {}
    records = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(records, dict):
        raise ValueError("Invalid incognito routing metadata, refusing ingress")
    return records


_OFFLOAD = object()


class _IngressScope:
    """Resolve routing without the shared executor when the published index answers.

    ``fast`` returns ``_OFFLOAD`` only when a blocking read (an unloaded session store)
    is unavoidable. Ingress must not queue behind unrelated jobs on the default executor,
    and the extra hop also delays received work past handover/drain admission windows.
    ContextVars are always entered in the calling task.
    """

    def __init__(self, resolve: Callable[[], tuple[str | None, bool] | None],
                 fast: Callable[[], object] | None = None):
        self._resolve = resolve
        self._fast = fast
        self._scope = None

    def _enter(self, decision):
        self._scope = _decision_scope(decision)
        return self._scope.__enter__()

    def __enter__(self):
        return self._enter(self._resolve())

    def __exit__(self, *exc):
        assert self._scope is not None
        return self._scope.__exit__(*exc)

    async def __aenter__(self):
        decision = self._fast() if self._fast is not None else _OFFLOAD
        if decision is _OFFLOAD:
            decision = await asyncio.to_thread(self._resolve)
        return self._enter(decision)

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


@contextmanager
def _decision_scope(decision):
    if decision is None:
        yield
        return
    key, enabled = decision
    token = _TOPIC.set(key or private_topic())
    try:
        with incognito_scope(enabled or transition_pending(key), key):
            yield
    finally:
        _TOPIC.reset(token)


def _unattributed_decision():
    # Unknown updates cannot establish that they are outside a private interval.
    return None, any_incognito_sessions() or bool(_TRANSITIONING) or bool(_routing_records())


def unattributed_event_scope():
    return _IngressScope(_unattributed_decision, _unattributed_decision)


def _event_decision(owner, event, *, blocking=True):
    source = getattr(event, "source", None)
    if source is None:
        return None
    canonicalize = getattr(owner, "_canonicalize", None)
    if callable(canonicalize):
        canonicalize(source)
    if getattr(source, "profile_route_rejected", False) is True:
        return None
    key_for = getattr(owner, "_session_key_for_source", None) or getattr(owner, "_source_session_key", None)
    key = key_for(source) if callable(key_for) else None
    enabled = is_incognito(key) if isinstance(key, str) else False
    store = getattr(owner, "session_store", None)
    if store is not None and isinstance(key, str):
        if blocking:
            decision = store.incognito_enabled(key)
        else:
            published = getattr(store, "published_incognito_enabled", None)
            decision = published(key) if callable(published) else None
            if decision is None:
                return _OFFLOAD
        if isinstance(decision, bool):
            enabled = decision
    elif isinstance(key, str):
        records = _routing_records()
        enabled = key in records or enabled
        if key in records:
            session_id = records[key]["session_id"]
            if not blocking and not (is_incognito(session_id) and is_incognito(key)):
                # Registering waits for in-flight persistence; keep that off the loop.
                return _OFFLOAD
            register_session(session_id, True)
            register_session(key, True)
    # The entry command and its confirmation must also avoid normal admission/result stores.
    command = getattr(event, "get_command", None)
    entering = callable(command) and command() == "incognito"
    return key if isinstance(key, str) else None, enabled or entering


def event_scope(owner, event):
    return _IngressScope(lambda: _event_decision(owner, event),
                         lambda: _event_decision(owner, event, blocking=False))


def slash_refusal(event) -> str | None:
    if not is_incognito():
        return None
    from hermes_cli.commands import resolve_command
    command = getattr(event, "get_command", None)
    definition = resolve_command(command() or "") if callable(command) else None
    name = definition.name if definition else ""
    args = (event.get_command_args() or "") if definition else ""
    durable = name in {"title", "branch", "resume", "sessions", "bg", "review", "refine",
                       "goal", "subgoal", "loop", "heartbeat", "plan", "pause", "personality"}
    global_route = name in {"model", "reasoning", "fast"} and "--global" in args.split()
    permanent_approval = name == "approve" and "always" in args.split()
    if durable or global_route or permanent_approval:
        return "This durable command is unavailable in incognito. Use /incognito off or /new first."
    return None


def private_ingress(fn):
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_scoped(self, event, *args, **kwargs):
            async with event_scope(self, event):
                return await fn(self, event, *args, **kwargs)
        return async_scoped
    @wraps(fn)
    def sync_scoped(self, event, *args, **kwargs):
        with event_scope(self, event):
            return fn(self, event, *args, **kwargs)
    return sync_scoped
