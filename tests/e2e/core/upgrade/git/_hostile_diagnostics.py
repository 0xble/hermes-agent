"""Bounded, passive diagnostics for the hostile Git E2E files.

The file runner owns the unchanged 900-second kill. Emit phase/log evidence before
that deadline without making a potentially stuck NamespaceHost RPC to collect it.
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.e2e.core.upgrade import _helpers as H

_PHASE = "not started"
_PATHS: tuple[Path, ...] = ()
_STREAM = None


def phase(label: str, *paths: Path) -> None:
    global _PHASE, _PATHS
    _PHASE = label
    if paths:
        _PATHS = paths
    mode = "bwrap" if H.BWRAP_OK else "no-bwrap CI fallback"
    print(f"[hostile phase {time.monotonic():.1f}] {mode}: {label}", file=_STREAM or sys.stderr, flush=True)


def snapshot() -> None:
    mode = "bwrap" if H.BWRAP_OK else "no-bwrap CI fallback"
    print(f"[hostile watchdog {time.monotonic():.1f}] {mode}: {_PHASE}", file=_STREAM or sys.stderr, flush=True)
    for path in _PATHS:
        try:
            # Never drain a child pipe or call the namespace agent from the watchdog.
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 4000))
                tail = stream.read(4000).decode("utf-8", errors="replace")
        except OSError as exc:
            tail = f"{type(exc).__name__}: {exc}"
        print(f"--- hostile watchdog {path} ---\n{tail}", file=_STREAM or sys.stderr, flush=True)


@pytest.fixture(scope="module", autouse=True)
def hostile_diagnostics(request):
    global _STREAM
    # Duplicate the original stderr while pytest capture is suspended: captured
    # fixture output would otherwise vanish when the file runner SIGKILLs pytest.
    capture = request.config.pluginmanager.getplugin("capturemanager")
    with capture.global_and_fixture_disabled():
        _STREAM = os.fdopen(os.dup(sys.stderr.fileno()), "w", encoding="utf-8")  # windows-footgun: ok - writes original stderr
    stopped = threading.Event()

    def watch():
        # Four snapshots (180/360/540/720s), all before the file runner's 900s bound.
        for _ in range(4):
            if stopped.wait(180):
                return
            snapshot()

    phase(request.node.nodeid)
    faulthandler.dump_traceback_later(240, repeat=True, file=_STREAM)
    watcher = threading.Thread(target=watch, name="hostile-phase-watchdog", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stopped.set()
        watcher.join(timeout=1)
        faulthandler.cancel_dump_traceback_later()
        phase(f"file finished: {request.node.nodeid}")
        _STREAM.close()
        _STREAM = None


@pytest.fixture(autouse=True)
def hostile_cell(request, hostile_diagnostics):
    phase(f"cell start: {request.node.nodeid}")
    try:
        yield
    finally:
        phase(f"cell finished: {request.node.nodeid}")
