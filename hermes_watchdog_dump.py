"""Small, import-light helpers for watchdog diagnostics."""

from __future__ import annotations

import sys
import threading
import traceback
from typing import TextIO


def write_main_thread_stack(file: TextIO) -> None:
    """Write the main thread's current Python stack before a faulthandler dump.

    This is deliberately best-effort and stdlib-only: the watchdog must not let
    diagnostic collection change the exit path it is protecting.
    """
    try:
        frames = sys._current_frames()
        main_ident = threading.main_thread().ident
        file.write(
            "Main thread (written first: faulthandler stops after 100 threads); "
            f"active threads: {threading.active_count()}\n"
        )
        frame = frames.get(main_ident) if main_ident is not None else None
        if frame is None:
            file.write("(main thread frame unavailable)\n")
            return
        file.writelines(traceback.format_stack(frame))
    except BaseException:
        # Watchdog diagnostics are never allowed to interfere with the exit path.
        return
