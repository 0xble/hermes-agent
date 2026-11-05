"""tui_gateway test fixtures.

Several files here import ``tui_gateway.server`` inside a ``patch.dict("sys.modules", {"hermes_constants":
MagicMock(...)})`` window so the module binds a fixed home. The server's import graph reaches
``agent.process_bootstrap`` → ``hermes_bootstrap``, which is process boot: PM dependency activation reads
the real install root through ``hermes_constants`` and exits the process when that is a MagicMock.
Importing it once here, before any window opens, keeps boot out of the mocked import.
"""

import hermes_bootstrap
import pytest


@pytest.fixture
def without_write_stall_sampler(monkeypatch):
    """Keep synchronous gateway thread doubles out of the native sampler.

    Patching ``server.threading.Thread`` changes the shared stdlib module. On
    macOS a state write can then run the watchdog's long-lived helper inline.
    These tests exercise TUI behavior, not sampling; keep real observations and
    writes, but don't launch a helper. Native lifecycle/sampling tests opt out
    simply by not requesting this fixture.
    """
    import hermes_state_write_stalls as stalls

    monkeypatch.setattr(stalls, "_watchdog", None)
    monkeypatch.setattr(stalls.WriteStallWatchdog, "start", lambda self: None)
    try:
        yield
    finally:
        if stalls._watchdog is not None:
            stalls._watchdog.close()
