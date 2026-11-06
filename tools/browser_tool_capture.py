"""Memory-only CLI output capture, without waiting for inherited daemon pipe EOF.

The CLI can fork a resident daemon that inherits stdout/stderr. Keep draining those
handles after the command ends (discarding later writes), so the daemon never sees
SIGPIPE. Collection waits for the CLI, not for that daemon to close its handles.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import BinaryIO, cast


def _available_read(fd: int) -> bytes:
    if sys.platform == "win32":
        import _winapi
        import msvcrt

        # Windows Python 3.11 has no nonblocking anonymous pipe support in os.
        try:
            available, _ = _winapi.PeekNamedPipe(msvcrt.get_osfhandle(fd), 0)
        except BrokenPipeError:
            return b""
        if not available:
            raise BlockingIOError
        return os.read(fd, min(65536, available))
    return os.read(fd, 65536)


class _Output:
    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.finish = threading.Event()
        self.ready = threading.Event()
        self.buffer = bytearray()
        self.overflow = False
        from agent.memory_provider import spawn_context_thread
        self.thread = spawn_context_thread(self._drain, name="browser-output-drain")
        if sys.platform != "win32":
            os.set_blocking(stream.fileno(), False)
        self.thread.start()

    def _drain(self) -> None:
        retaining = True
        drained = 0
        try:
            while True:
                try:
                    data = _available_read(self.stream.fileno())
                except BlockingIOError:
                    if self.finish.is_set():
                        retaining = False
                        self.ready.set()
                    if retaining:
                        self.finish.wait(.01)
                    else:
                        # Do not spin on an idle resident daemon's inherited handle.
                        threading.Event().wait(.05)
                    continue
                if not data:
                    break
                if retaining:
                    # Page-controlled output cannot grow the worker without bound.
                    if len(self.buffer) + len(data) <= 16 << 20:
                        self.buffer.extend(data)
                    else:
                        self.overflow = True
                # A hostile daemon can keep writing forever; final draining is bounded.
                if self.finish.is_set():
                    drained += len(data)
                    if drained >= 1 << 20:
                        retaining = False
                        self.ready.set()
        except OSError:
            pass
        finally:
            self.stream.close()
            self.ready.set()

    def take(self) -> str:
        self.finish.set()
        if not self.ready.wait(2):
            # No raw output escapes on an internal collector failure.
            raise RuntimeError("Browser CLI memory capture did not finish")
        value = bytes(self.buffer).decode("utf-8-sig", errors="replace").strip()
        self.buffer.clear()
        if self.overflow:
            raise RuntimeError("Browser CLI output exceeded the 16 MiB in-memory capture limit")
        return value


def wait_for_cli(proc: subprocess.Popen, timeout: float, payload: bytes | None = None) -> None:
    """One deadline for in-memory stdin delivery and CLI exit; never wait for output EOF."""
    deadline = time.monotonic() + timeout
    if payload is not None:
        stream = proc.stdin
        assert stream is not None
        fd = stream.fileno()
        if sys.platform == "win32":
            import _winapi
            import msvcrt
            handle = msvcrt.get_osfhandle(fd)
            _winapi.SetNamedPipeHandleState(handle, 1, None, None)  # PIPE_NOWAIT, Python 3.11 too
            def write(data):
                return _winapi.WriteFile(handle, data)[0]
        else:
            os.set_blocking(fd, False)
            def write(data):
                return os.write(fd, data)
        try:
            remaining = memoryview(payload)
            while remaining:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(proc.args, timeout)
                try:
                    written = write(remaining[:65536])
                    remaining = remaining[written:]
                    if written:
                        continue
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    break
                time.sleep(.01)
        finally:
            stream.close()
            proc.stdin = None
    proc.wait(timeout=max(0, deadline - time.monotonic()))


class BrowserOutputCapture:
    def __init__(self, proc: subprocess.Popen):
        assert proc.stdout is not None and proc.stderr is not None
        self.outputs = (_Output(cast(BinaryIO, proc.stdout)), _Output(cast(BinaryIO, proc.stderr)))
        # The capture owns the output handles; CLI waiting must never launch
        # another reader or wait for a daemon's inherited pipe EOF.
        proc.stdout = proc.stderr = None

    def take(self) -> tuple[str, str]:
        for output in self.outputs:
            output.finish.set()
        values, failure = [], None
        for output in self.outputs:
            try:
                values.append(output.take())
            except RuntimeError as exc:
                # Always release the sibling buffer too, including overflow paths.
                failure = failure or exc
        if failure:
            raise failure
        return values[0], values[1]


def collect(proc: subprocess.Popen) -> tuple[str, str]:
    """Collect once, after CLI exit/timeout, while daemon handles remain drainable."""
    return getattr(proc, "_hermes_browser_capture").take()
