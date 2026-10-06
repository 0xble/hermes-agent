"""Thread-scoped stdout/stderr silencing for background worker threads.

``contextlib.redirect_stdout`` reassigns the *process-global* stream, so a daemon worker
silencing itself also silences every other thread (gateway event loop included). This
module installs a per-thread routing proxy as ``sys.stdout``/``sys.stderr``: silenced
threads write to a sink, everyone else passes through to the original stream. Installed
once, idempotently, and never uninstalled (that would race other threads mid-write).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from typing import Any, Iterator, TextIO

__all__ = [
    "thread_scoped_silence", "adopt_routing_proxy", "delegate_getattr", "is_stdio_wrapper", "resolve_stdio", "stdio_chain", "stdio_install_lock",
]

_install_lock = threading.Lock()
# Every installer that rebinds sys.stdout/sys.stderr holds this, so a concurrent agent build
# and silence install cannot each read the old stream and stack over one another.
stdio_install_lock = _install_lock
# Proxy installed per attribute ("stdout"/"stderr"): never double-wrap.
_installed: dict[str, "_ThreadRoutingStream"] = {}
# One process-lifetime sink per stream: global redirects that displace and
# restore a proxy must not leak a new /dev/null descriptor each time.
_sinks: dict[str, TextIO] = {}
_routing_states: dict[str, "_RoutingState"] = {}


def is_stdio_wrapper(stream: object) -> bool:
    """True for a Hermes stdio wrapper (one that defines ``_hermes_stdio_next``).

    Looked up on the type, so a wrapper's delegating ``__getattr__`` never runs.
    """
    return hasattr(type(stream), "_hermes_stdio_next")


def stdio_chain(stream: object) -> Iterator[object]:
    """Yield ``stream`` and each Hermes wrapper layer under it, down to the real stream.

    Iterative and cycle-safe: the walk stops at the first repeated layer.
    """
    seen: set[int] = set()
    while stream is not None and id(stream) not in seen:
        seen.add(id(stream))
        yield stream
        step = getattr(type(stream), "_hermes_stdio_next", None)
        stream = step(stream) if step is not None else None


def resolve_stdio(stream: object) -> Any:
    """The real stream a Hermes wrapper chain delegates to, or None if the chain never reaches one."""
    last = None
    for last in stdio_chain(stream):
        pass
    return None if last is None or is_stdio_wrapper(last) else last


def delegate_getattr(wrapper: object, name: str, own_slots: tuple[str, ...]) -> Any:
    """Shared ``__getattr__`` for Hermes stdio wrappers.

    Resolves the real stream iteratively rather than asking the next layer, so a deep or cyclic
    chain raises AttributeError instead of recursing until RecursionError. A wrapper's own
    unset slots (e.g. on a copy) raise directly so resolution never re-enters ``__getattr__``.
    """
    if name in own_slots:
        raise AttributeError(name)
    real = resolve_stdio(wrapper)
    if real is None:
        raise AttributeError(f"{type(wrapper).__name__} wraps no real stream, so it has no {name!r}")
    return getattr(real, name)


def _real_stream(attr: str, candidate: object) -> Any:
    """``candidate`` unwrapped to its real stream, else the interpreter's original stream."""
    real = resolve_stdio(candidate)
    return real if real is not None else getattr(sys, f"__{attr}__", None)


class _RoutingState:
    """Silencing registry shared by every proxy generation for one stream."""

    def __init__(self, sink: TextIO) -> None:
        self.sink = sink
        self.silenced: dict[int, int] = {}
        self.lock = threading.Lock()


class _ThreadRoutingStream:
    """``sys.stdout``/``sys.stderr`` stand-in routing writes per calling thread;
    unknown attributes delegate to the current thread's target."""

    def __init__(self, passthrough: TextIO, state: _RoutingState) -> None:
        # Bind the real stream, never another wrapper, so no chain can loop back to this proxy.
        self._passthrough = resolve_stdio(passthrough)
        self._state = state

    def _target(self) -> TextIO:
        return self._state.sink if self._state.silenced.get(threading.get_ident(), 0) > 0 else self._passthrough

    def silence(self, ident: int) -> None:
        with self._state.lock:
            self._state.silenced[ident] = self._state.silenced.get(ident, 0) + 1

    def unsilence(self, ident: int) -> None:
        with self._state.lock:
            depth = self._state.silenced.get(ident, 0) - 1
            if depth > 0:
                self._state.silenced[ident] = depth
            else:
                self._state.silenced.pop(ident, None)

    def _forward(self, name: str, fallback, *args):  # type: ignore[no-untyped-def]
        """Call ``name`` on the current target; a dead target yields ``fallback(*args)`` instead of raising."""
        try:
            return getattr(self._target(), name)(*args)
        except Exception:
            return fallback(*args)

    def write(self, data):  # type: ignore[no-untyped-def]
        return self._forward("write", lambda d: len(d) if isinstance(d, str) else 0, data)

    def flush(self):  # type: ignore[no-untyped-def]
        return self._forward("flush", lambda: None)

    def writelines(self, lines):  # type: ignore[no-untyped-def]
        return self._forward("writelines", lambda _l: None, lines)

    def isatty(self) -> bool:
        try:
            return bool(self._target().isatty())
        except Exception:
            return False

    def fileno(self):  # type: ignore[no-untyped-def]
        return self._target().fileno()

    def _hermes_stdio_next(self):  # type: ignore[no-untyped-def]
        # The thread-independent stream underneath, never the calling thread's sink, so chain
        # resolution from a silenced thread cannot bind devnull as a passthrough.
        return self.__dict__.get("_passthrough")

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        if name in ("_passthrough", "_state"):
            raise AttributeError(name)
        state = self.__dict__.get("_state")
        if state is not None and state.silenced.get(threading.get_ident(), 0) > 0:
            return getattr(state.sink, name)
        return delegate_getattr(self, name, ())


def adopt_routing_proxy(attr: str, current: object, fallback: object = None) -> "_ThreadRoutingStream | None":
    """Collapse ``current``'s chain onto the routing proxy in it, if any, and return that proxy.

    The proxy goes back on top as ``sys.<attr>`` and its passthrough is rebound to the resolved
    real stream, so a long chain left by earlier generations (or a cycle) shrinks to one layer.
    Callers hold ``stdio_install_lock``.
    """
    for layer in stdio_chain(current):
        if isinstance(layer, _ThreadRoutingStream):
            real = resolve_stdio(layer.__dict__.get("_passthrough"))
            layer._passthrough = real if real is not None else _real_stream(attr, fallback)
            if current is not layer:
                setattr(sys, attr, layer)  # the proxy guards its own writes; drop wrappers above it
            _installed[attr] = layer
            _routing_states[attr] = layer._state
            return layer
    return None


def _ensure_installed(attr: str, passthrough: TextIO) -> "_ThreadRoutingStream":
    """Install (idempotently) a routing proxy as ``sys.<attr>`` and return it."""
    with _install_lock:
        current = getattr(sys, attr, None)
        # Adopting a proxy already in the chain (one an agent build wrapped, or a redirect
        # restored) instead of installing over it keeps the chain from growing per call.
        adopted = adopt_routing_proxy(attr, current, passthrough)
        if adopted is not None:
            return adopted
        # Route non-silenced threads to whatever is currently bound (an active global redirect
        # keeps its old behavior), unwrapped to the real stream.
        passthrough = _real_stream(attr, current if current is not None else passthrough)
        sink = _sinks.get(attr)
        if sink is None or sink.closed:
            sink = _sinks[attr] = open(os.devnull, "w", encoding="utf-8")
        state = _routing_states.get(attr)
        if state is None or state.sink is not sink:
            state = _routing_states[attr] = _RoutingState(sink)
        proxy = _installed[attr] = _ThreadRoutingStream(passthrough, state)
        setattr(sys, attr, proxy)
        return proxy


@contextlib.contextmanager
def thread_scoped_silence() -> Iterator[None]:
    """Silence ``stdout``/``stderr`` for the *current thread only*."""
    ident = threading.get_ident()
    proxies = (_ensure_installed("stdout", sys.__stdout__ or sys.stdout), _ensure_installed("stderr", sys.__stderr__ or sys.stderr))
    for proxy in proxies:
        proxy.silence(ident)
    try:
        yield
    finally:
        for proxy in proxies:
            proxy.unsilence(ident)
