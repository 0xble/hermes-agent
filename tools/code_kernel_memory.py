"""Local kernel memory policy and cheap process-tree sampling; no tool registration."""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from functools import lru_cache

import psutil

logger = logging.getLogger(__name__)
DEFAULT_KERNEL_MEMORY_LIMIT_MB = 8192
_MEMORY_SAMPLE_SECONDS = 1.0
_MIB = 1024 ** 2


def kernel_memory_limit_bytes() -> int:
    """Invalid/nonpositive values retain the default-on safety ceiling."""
    from tools.code_execution_tool import _load_config
    raw = _load_config().get("kernel_memory_limit_mb", DEFAULT_KERNEL_MEMORY_LIMIT_MB)
    try:
        mb = int(raw) if isinstance(raw, (int, str)) and not isinstance(raw, bool) else 0
        if not 0 < mb <= sys.maxsize // _MIB:
            raise ValueError("invalid kernel memory ceiling")
    except (TypeError, ValueError, OverflowError):
        mb = DEFAULT_KERNEL_MEMORY_LIMIT_MB
    return mb * _MIB


class _RUsageInfoV0(ctypes.Structure):
    # Stable Darwin sys/resource.h ABI (RUSAGE_INFO_V0). Unlike RSS, phys_footprint
    # charges compressed memory too, so compression cannot hide a runaway cell.
    _fields_ = [("uuid", ctypes.c_uint8 * 16), ("counters", ctypes.c_uint64 * 10)]


@lru_cache(maxsize=1)
def _darwin_rusage():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    fn = lib.proc_pid_rusage
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    fn.restype = ctypes.c_int
    return fn


def darwin_physical_footprint(pid: int) -> int:
    usage = _RUsageInfoV0()
    if _darwin_rusage()(pid, 0, ctypes.byref(usage)) != 0:
        raise OSError(ctypes.get_errno(), "kernel footprint unavailable")
    return int(usage.counters[7])  # ri_phys_footprint


def process_memory_bytes(process: psutil.Process) -> int:
    rss = process.memory_info().rss
    if sys.platform == "darwin":
        try:
            return max(rss, darwin_physical_footprint(process.pid))
        except (OSError, AttributeError):
            logger.debug("kernel physical footprint unavailable; using RSS")
    return rss


class KernelMemoryWatch:
    """One sample/second during a cell, plus a final sample for fast allocations."""

    def __init__(self, pid: int, limit_bytes: int):
        self.limit_bytes = limit_bytes
        self.next_sample = 0.0
        self.process = None
        self.warned = False
        try:
            self.process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            pass

    def exceeded(self, *, final: bool = False) -> int:
        now = time.monotonic()
        if not final and now < self.next_sample:
            return 0
        self.next_sample = now + _MEMORY_SAMPLE_SECONDS
        process = self.process
        if process is None or not process.is_running():
            return 0
        total = 0
        try:
            # psutil Process objects carry creation times: is_running rejects reused
            # PIDs. Never sample unrelated processes or remote PIDs on the host.
            for member in [process, *process.children(recursive=True)]:
                try:
                    if member.is_running():
                        total += process_memory_bytes(member)
                except psutil.NoSuchProcess:
                    continue  # Descendants can exit between enumeration and sampling.
        except (OSError, psutil.Error):
            if not self.warned:
                logger.warning("kernel process-tree memory sample unavailable (details omitted)")
                self.warned = True
        return total if total > self.limit_bytes else 0


# Applied IN the Linux child, before watchdog threads/user code, not preexec_fn
# (which can deadlock a threaded parent). macOS rlimits do not enforce this reliably.
# Linux limits are per process; the host watchdog still bounds the whole tree.
RUNNER_MEMORY_SOURCE = '''\
_memory_rlimit_bytes = 0
if sys.platform.startswith("linux"):
    import resource
    _memory_limit = int(sys.argv[1])
    for _kind in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
        try:
            _soft, _hard = resource.getrlimit(_kind)
            _hard = _memory_limit if _hard == resource.RLIM_INFINITY else min(_hard, _memory_limit)
            _soft = _hard if _soft == resource.RLIM_INFINITY else min(_soft, _hard)
            resource.setrlimit(_kind, (_soft, _hard))
            _memory_rlimit_bytes = min(_memory_rlimit_bytes or _soft, _soft)
        except (OSError, ValueError, OverflowError):
            print("kernel memory rlimit unavailable; host watchdog remains active", file=sys.stderr)
'''
