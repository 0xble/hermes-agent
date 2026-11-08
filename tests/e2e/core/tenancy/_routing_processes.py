"""Case-owned process teardown, including gateways detached into another session."""
from __future__ import annotations

import os
from pathlib import Path

import psutil

from tests.waiting import wait_until


def case_processes(root: Path) -> list[psutil.Process]:
    """Live same-user processes whose explicit HERMES_HOME is inside this case."""
    lexical_root = Path(os.path.abspath(root))
    root = root.resolve()
    owned = []
    username = psutil.Process().username()
    for proc in psutil.process_iter(["username", "status"], ad_value=None):
        try:
            if (proc.info["username"] != username
                    or proc.info["status"] == psutil.STATUS_ZOMBIE):
                continue
            home = proc.environ().get("HERMES_HOME")
            if not home:
                continue
            path = Path(os.path.abspath(home))
            # Do not stat/resolve any unrelated home (the real-home I/O guard enforces this).
            if not (path.is_relative_to(lexical_root) or path.is_relative_to(root)):
                continue
            if path.resolve().is_relative_to(root):
                owned.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return owned


def stop_case_processes(root: Path) -> None:
    """Stop only explicit case-home owners, including startup before PID publication.

    Gateway/service discovery is deliberately not used: it could select the host's real owner.
    PID files alone miss gateways still importing when run_hermes times out. The same case-home
    check used by the regression grants cleanup authority; psutil handles guard against PID reuse.
    """
    canonical_root = root.resolve()
    # A child can be published while its owner handles SIGTERM. Rescan only after those owners
    # are stopped, so the second pass also owns late detached children.
    for _ in range(2):
        processes = case_processes(root)
        if not processes:
            return
        for proc in processes:
            try:
                if proc.status() == psutil.STATUS_ZOMBIE:
                    continue
                home = proc.environ().get("HERMES_HOME")
                assert home and Path(home).resolve().is_relative_to(canonical_root), (
                    f"refusing PID {proc.pid} outside case {root}: HERMES_HOME={home!r}")
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs(processes, timeout=10)
        for proc in alive:
            try:
                if proc.status() == psutil.STATUS_ZOMBIE:
                    continue
                home = proc.environ().get("HERMES_HOME")
                assert home and Path(home).resolve().is_relative_to(canonical_root), (
                    f"refusing PID {proc.pid} outside case {root}: HERMES_HOME={home!r}")
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=5)
    wait_until(lambda: not case_processes(root), timeout=15,
               what=f"all processes with HERMES_HOME under {root} to stop")
