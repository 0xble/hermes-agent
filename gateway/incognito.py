"""Ingress scoping and RAM-only equivalents for gateway durability stores."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import sqlite3
import threading

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


@contextmanager
def unattributed_event_scope():
    """Unknown updates cannot establish that they are outside a private interval."""
    enabled = any_incognito_sessions() or bool(_TRANSITIONING) or bool(_routing_records())
    with incognito_scope(enabled):
        yield


@contextmanager
def event_scope(owner, event):
    source = getattr(event, "source", None)
    if source is None:
        yield
        return
    canonicalize = getattr(owner, "_canonicalize", None)
    if callable(canonicalize):
        canonicalize(source)
    if getattr(source, "profile_route_rejected", False) is True:
        yield
        return
    key_for = getattr(owner, "_session_key_for_source", None) or getattr(owner, "_source_session_key", None)
    key = key_for(source) if callable(key_for) else None
    enabled = is_incognito(key) if isinstance(key, str) else False
    store = getattr(owner, "session_store", None)
    if store is not None and isinstance(key, str):
        decision = store.incognito_enabled(key)
        if isinstance(decision, bool):
            enabled = decision
    elif isinstance(key, str):
        records = _routing_records()
        enabled = key in records or enabled
        if key in records:
            register_session(records[key]["session_id"], True)
            register_session(key, True)
    # The entry command and its confirmation must also avoid normal admission/result stores.
    command = getattr(event, "get_command", None)
    entering = callable(command) and command() == "incognito"
    if not isinstance(key, str):
        key = None
    token = _TOPIC.set(key or private_topic())
    try:
        with incognito_scope(enabled or entering or transition_pending(key), key):
            yield
    finally:
        _TOPIC.reset(token)


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
            with event_scope(self, event):
                return await fn(self, event, *args, **kwargs)
        return async_scoped
    @wraps(fn)
    def sync_scoped(self, event, *args, **kwargs):
        with event_scope(self, event):
            return fn(self, event, *args, **kwargs)
    return sync_scoped
