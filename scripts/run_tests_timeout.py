"""Best-effort Python stack snapshots for a per-file pytest timeout (POSIX)."""

from __future__ import annotations

import faulthandler
import gc
import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import AsyncGeneratorType, CoroutineType, GeneratorType

_DUMP_SIGNAL = getattr(signal, "SIGUSR2", None)
_STACK_FILE = "timeout-stacks.log"
_READY_FILE = "timeout-stacks.ready"


def _task_frames(task):
    # Task.get_stack() exposes only one frame for a suspended coroutine. Follow
    # its await chain too, without running anything on the potentially stuck loop.
    obj = task.get_coro()
    for _ in range(32):
        if isinstance(obj, CoroutineType):
            frame, obj = obj.cr_frame, obj.cr_await
        elif isinstance(obj, GeneratorType):
            frame, obj = obj.gi_frame, obj.gi_yieldfrom
        elif isinstance(obj, AsyncGeneratorType):
            frame, obj = obj.ag_frame, obj.ag_await
        else:
            break
        if frame is not None:
            yield f'  File "{frame.f_code.co_filename}", line {frame.f_lineno}, in {frame.f_code.co_name}\n'


def _dump_asyncio(fd: int) -> None:
    def write(text):
        os.write(fd, text.encode("utf-8", errors="backslashreplace"))

    write("\n=== asyncio task stacks ===\n")
    asyncio = sys.modules.get("asyncio")
    task_type = getattr(asyncio, "Task", None)
    count = 0
    if task_type is not None:
        # The running loop lookup is thread-local. Find tasks on every loop
        # (including background threads), without patching asyncio at startup.
        for task in gc.get_objects():
            if not issubclass(type(task), task_type) or task.done() or not task.get_loop().is_running():
                continue
            write(f"Task {task.get_name()!r}:\n")
            for line in _task_frames(task):
                write(line)
            count += 1
            if count == 100:
                write("(task dump limited to 100 tasks)\n")
                break
    if not count:
        write("(no live asyncio tasks found)\n")


def install_stack_dump(attempt_dir: str) -> None:
    if _DUMP_SIGNAL is None:
        return
    root = Path(attempt_dir)
    stream = (root / _STACK_FILE).open("wb", buffering=0)

    def dump_tasks(signum, frame):
        # Keep the stream alive through interpreter shutdown. faulthandler
        # writes the thread stacks from its native handler first; this Python
        # callback may not run if the main thread is wedged in native code.
        _dump_asyncio(stream.fileno())

    signal.signal(_DUMP_SIGNAL, dump_tasks)
    faulthandler.register(_DUMP_SIGNAL, file=stream, all_threads=True, chain=True)
    (root / _READY_FILE).touch()


def run_pytest() -> None:
    """Child bootstrap, preserving pytest as a separate argv token for ancestry checks."""
    attempt_dir, module = sys.argv[1:3]
    del sys.argv[1:3]
    sys.path[0] = os.getcwd()  # match python -m pytest
    install_stack_dump(attempt_dir)
    runpy.run_module(module, run_name="__main__", alter_sys=True)


def communicate_with_stack_dump(proc, file_timeout: float, attempt_dir: str):
    """Request stacks near the end of the budget without extending the hard cap."""
    if _DUMP_SIGNAL is None or file_timeout <= 0:
        return proc.communicate(timeout=file_timeout)
    lead = min(1.0, file_timeout / 4)
    deadline = time.monotonic() + file_timeout
    try:
        return proc.communicate(timeout=file_timeout - lead)
    except subprocess.TimeoutExpired:
        if proc.returncode is None:
            _request_stack_dump(proc.pid, attempt_dir)
        return proc.communicate(timeout=max(0.0, deadline - time.monotonic()))


def _request_stack_dump(pid: int, attempt_dir: str) -> None:
    # An unregistered child would terminate on SIGUSR2. Signal only the test
    # process, not descendants, which need not have installed the handler.
    if _DUMP_SIGNAL is not None and (Path(attempt_dir) / _READY_FILE).is_file():
        try:
            os.kill(pid, _DUMP_SIGNAL)
        except OSError:
            pass  # The child may have exited; normal tree cleanup still runs.


_STACK_DUMP_MAX_BYTES = 256 * 1024


def read_stack_dump(attempt_dir: str) -> str:
    try:
        with open(Path(attempt_dir) / _STACK_FILE, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _STACK_DUMP_MAX_BYTES))
            raw = fh.read(_STACK_DUMP_MAX_BYTES)
    except OSError:
        return ""
    stacks = raw.decode("utf-8-sig", errors="replace")
    if size > _STACK_DUMP_MAX_BYTES:
        stacks = f"[stack dump truncated to last {_STACK_DUMP_MAX_BYTES} bytes of {size}]\n" + stacks
    if stacks:
        return "\n=== Python thread stacks before file timeout ===\n" + stacks
    return ""


if __name__ == "__main__":
    run_pytest()
