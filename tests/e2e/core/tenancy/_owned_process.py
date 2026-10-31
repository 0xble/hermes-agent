"""POSIX test-process custody, independent of gateway idle-exit policy.

The guardian outlives a killed pytest parent. The real CLI/TUI and the gateways it
starts share a private group, killed in finally on return, timeout or parent death.
Only this test entry point suppresses the production daemon's deliberate setsid.
"""
from __future__ import annotations

import os
import runpy
import signal
import subprocess
import sys
from pathlib import Path


def main() -> int:
    mode, module, *args = sys.argv[1:]
    if mode == "run":
        from hermes_cli import gateway_runtime_start
        gateway_runtime_start.windows_detach_popen_kwargs = dict
        sys.argv = [module, *args]
        runpy.run_module(module, run_name="__main__")
        return 0

    owner = int(mode)
    if os.getppid() != owner:
        return 1

    def terminated(_signum, _frame):
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, terminated)
    child = None
    try:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", module, *args],
            start_new_session=True,
        )
        while os.getppid() == owner:
            try:
                return child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
        return 1
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
