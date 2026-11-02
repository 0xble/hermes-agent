"""Observation-only write phases; a separate interpreter can sample without our GIL.

Phase publication touches only a preallocated shared mapping. File I/O, process
startup, retention and warning emission all happen outside the writer guard.
"""
from __future__ import annotations

import atexit
import contextvars
import itertools
import json
import logging
import mmap
import os
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("hermes_state")
_STAMP = struct.Struct("=QdQ8s")
_SLOT_SIZE = 2048
_SLOTS = 64
_COOLDOWN_S = 600.0
current_observation: contextvars.ContextVar[WriteObservation] = contextvars.ContextVar("state_write_observation")


class WriteStallWatchdog:
    """One process-wide cooldown and bounded slots shared with a read-only helper."""

    def __init__(self, threshold_s=10.0):
        self.threshold_s = threshold_s
        self.pid = os.getpid()
        # Unlinked immediately; no phase publication writes or flushes a file.
        self._file = tempfile.TemporaryFile(prefix="hermes-write-phases-")
        self._file.truncate(_SLOT_SIZE * _SLOTS)
        self._map = mmap.mmap(self._file.fileno(), _SLOT_SIZE * _SLOTS)
        self._slots = set()
        self._sequence = itertools.count(1)
        self._sampled = set()
        self._last_capture = float("-inf")
        self._lock = threading.Lock()
        self._read_fd, self._stop_fd = os.pipe()
        self._thread = None
        self._ready = threading.Event()

    def reserve(self, operation, directory=None):
        """Reserve metadata before taking any database or writer lock."""
        if directory is None:
            from hermes_constants import get_hermes_home
            directory = get_hermes_home() / "logs" / "write-stalls"
        metadata = json.dumps([str(directory), operation[:160]]).encode("utf-8")
        if len(metadata) > _SLOT_SIZE - _STAMP.size:
            return None
        with self._lock:
            index = next((i for i in range(_SLOTS) if i not in self._slots), None)
            if index is None:
                return None
            self._slots.add(index)
            offset = index * _SLOT_SIZE + _STAMP.size
            self._map[offset:offset + _SLOT_SIZE - _STAMP.size] = metadata.ljust(_SLOT_SIZE - _STAMP.size, b"\0")
        return index

    def phase(self, slot, phase):
        if slot is not None:
            offset = slot * _SLOT_SIZE
            self._map[offset:offset + 8] = b"\0" * 8
            stamp = _STAMP.pack(next(self._sequence), time.monotonic(), threading.get_native_id(), phase.encode("ascii"))
            self._map[offset + 8:offset + _STAMP.size] = stamp[8:]
            self._map[offset:offset + 8] = stamp[:8]

    def begin(self, phase, operation):
        slot = self.reserve(operation)
        self.phase(slot, phase)
        return slot

    def end(self, slot):
        if slot is not None:
            offset = slot * _SLOT_SIZE
            self._map[offset:offset + 8] = b"\0" * 8

    def release(self, slot):
        self.end(slot)
        with self._lock:
            self._slots.discard(slot)

    def poll(self, now):
        active = set()
        for index in range(_SLOTS):
            offset = index * _SLOT_SIZE
            stamp = bytes(self._map[offset:offset + _STAMP.size])
            sequence, started, tid, phase = _STAMP.unpack(stamp)
            if not sequence:
                continue
            active.add(sequence)
            if sequence in self._sampled or now - started <= self.threshold_s or now - self._last_capture < _COOLDOWN_S:
                continue
            metadata = bytes(self._map[offset + _STAMP.size:offset + _SLOT_SIZE])
            if stamp != self._map[offset:offset + _STAMP.size]:
                continue
            directory, operation = json.loads(metadata.rstrip(b"\0"))
            self._sampled.add(sequence)
            self._last_capture = now
            self._capture(Path(directory), tid, phase.rstrip(b"\0").decode("ascii"), operation, sequence, offset, stamp)
        self._sampled.intersection_update(active)

    def _capture(self, directory, tid, phase, operation, sequence, offset, stamp):
        path = directory / f"write-stall-{self.pid}-{tid}-{time.time_ns()}-{sequence}.txt"
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Reserve private permissions before sample opens its output.
            path.touch(mode=0o600, exist_ok=False)
            if stamp != self._map[offset:offset + _STAMP.size]:
                path.unlink(missing_ok=True)
                return
            subprocess.run(["/usr/bin/sample", str(self.pid), "1", "-file", str(path)],
                           check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            samples = sorted(directory.glob("write-stall-*.txt"), key=lambda item: item.stat().st_mtime_ns)
            for old in samples[:-20]:
                old.unlink(missing_ok=True)
            logger.warning("state.db native stack captured: pid=%d native_tid=%d phase=%s operation=%s sample=%s",
                           self.pid, tid, phase, operation, path)
        except (OSError, subprocess.SubprocessError) as error:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # A failed diagnostic cleanup must not disable later observations.
            logger.warning("state.db native stack capture failed: pid=%d error_type=%s", self.pid, type(error).__name__)

    def start(self):
        """Spawn from a daemon thread, never the lock-holding thread."""
        self._thread = threading.Thread(target=self._serve, name="state-write-stalls", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)

    def _serve(self):
        read_fd = self._read_fd
        assert read_fd is not None
        try:
            with subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watch",
                                   str(self._file.fileno()), str(read_fd), str(self.pid)],
                                  pass_fds=(self._file.fileno(), read_fd), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL) as child:
                os.close(read_fd)
                read_fd = self._read_fd = None
                self._ready.set()
                if child.wait() != 0:
                    logger.warning("state.db native stack capture failed: pid=%d error_type=HelperExit", self.pid)
        except OSError as error:
            logger.warning("state.db native stack capture failed: pid=%d error_type=%s", self.pid, type(error).__name__)
        finally:
            self._ready.set()
            if read_fd is not None:
                os.close(read_fd)
                self._read_fd = None

    def close(self):
        if self._stop_fd is not None:
            os.close(self._stop_fd)
            self._stop_fd = None
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=6)
        if self._read_fd is not None:
            os.close(self._read_fd)
            self._read_fd = None
        self._map.close()
        self._file.close()


def _watch(fd, stop_fd, pid):
    watcher = WriteStallWatchdog.__new__(WriteStallWatchdog)
    watcher.threshold_s = 10.0
    watcher.pid = pid
    watcher._map = mmap.mmap(fd, _SLOT_SIZE * _SLOTS, access=mmap.ACCESS_READ)
    watcher._sampled = set()
    watcher._last_capture = float("-inf")
    try:
        # EOF ties lifetime to the parent, even if a PID is subsequently reused.
        while not select.select([stop_fd], [], [], 0.5)[0]:
            watcher.poll(time.monotonic())
    finally:
        watcher._map.close()
        os.close(fd)
        os.close(stop_fd)


_watchdog = None
_watchdog_lock = threading.Lock()
_start_failure_at = float("-inf")


def _get_watchdog():
    global _watchdog, _start_failure_at
    from hermes_platform.host.facts import os_family
    if os_family() != "darwin":
        return None
    with _watchdog_lock:
        if _watchdog is not None and _watchdog.pid == os.getpid():
            return _watchdog
        if time.monotonic() - _start_failure_at < _COOLDOWN_S:
            return None
        candidate = None
        try:
            candidate = WriteStallWatchdog()
            candidate.start()
            _watchdog = candidate
            atexit.register(candidate.close)
            return candidate
        except (OSError, RuntimeError) as error:
            if candidate is not None:
                candidate.close()
            _watchdog = None
            _start_failure_at = time.monotonic()
            logger.warning("state.db native stack capture failed: error_type=%s", type(error).__name__)
            return None


class WriteObservation:
    def __init__(self, db_path, fn):
        name = getattr(fn, "__qualname__", type(fn).__qualname__)
        operation = getattr(fn, "_state_operation", None)
        self.operation = (operation if isinstance(operation, str) else name.split(".<locals>.")[0])[:160]
        self.wal_path = Path(str(db_path) + "-wal")
        self.warnings = []
        self.watchdog = _get_watchdog()
        self.slot = self.watchdog.reserve(self.operation) if self.watchdog is not None else None

    def phase(self, phase):
        if self.watchdog is not None:
            self.watchdog.phase(self.slot, phase)

    def end(self):
        if self.watchdog is not None:
            self.watchdog.end(self.slot)

    def flush(self):
        warnings, self.warnings = self.warnings, []
        for warning_logger, message, args in warnings:
            try:
                wal_bytes = self.wal_path.stat().st_size
            except FileNotFoundError:
                wal_bytes = 0
            except OSError:
                wal_bytes = "unknown"
            warning_logger.warning(message + " wal_bytes=%s", *args, wal_bytes)

    def finish(self):
        if self.watchdog is not None:
            self.watchdog.release(self.slot)
        self.flush()


@contextmanager
def write_attempt():
    try:
        yield
    finally:
        current_observation.get().flush()


@contextmanager
def observe_write(db_path, fn):
    observation = WriteObservation(db_path, fn)
    token = current_observation.set(observation)
    try:
        yield
    finally:
        current_observation.reset(token)
        observation.finish()


if __name__ == "__main__":
    _watch(*(int(value) for value in sys.argv[2:]))
