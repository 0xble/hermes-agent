"""Retain bounded Windows protocol cell timings, including a file-timeout boundary."""
import time

import pytest


_TIMED_FILES = {
    "test_desktop_update_windows_handoff_lineage.py",
    "test_desktop_update_windows_marker.py",
    "test_desktop_update_windows_marker_lock.py",
}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if item.path.name not in _TIMED_FILES:
        yield
        return
    started = time.monotonic()
    print(f"[windows-protocol] start {started:.3f} {item.nodeid}", flush=True)
    try:
        yield
    finally:
        print(f"[windows-protocol] finish {time.monotonic():.3f} "
              f"elapsed={time.monotonic() - started:.3f}s {item.nodeid}", flush=True)
