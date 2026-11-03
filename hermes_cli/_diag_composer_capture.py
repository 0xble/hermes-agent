"""Disposable opt-in snapshot observer, never a qualification or runtime patch."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path


async def observe_composer(owner, app, output: str) -> None:
    """Read state on the application loop only when requested, never redraw."""
    request = Path(output + ".req")
    while True:
        spec = None
        try:
            if request.exists():
                spec = json.loads(request.read_text(encoding="utf-8"))
                request.unlink()
                buffer = app.current_buffer
                names = (
                    "_agent_running", "_command_running", "_approval_state", "_clarify_state",
                    "_sudo_state", "_secret_state", "_connection_state", "_slash_confirm_state",
                    "_clarify_freetext", "_voice_recording", "_voice_processing", "_voice_mode",
                    "_model_picker_state", "_command_palette_state", "_shutting_down",
                )
                state = {
                    "status": "snapshot", "monotonic": time.monotonic(), "wall_time": time.time(),
                    "pid": os.getpid(), "thread_id": threading.get_ident(),
                    "thread_name": threading.current_thread().name,
                    "monotonic_ns": time.monotonic_ns(), "wall_time_ns": time.time_ns(), "request": spec,
                    "buffer_text": buffer.text, "cursor_position": buffer.cursor_position,
                    "buffer_name": buffer.name, "prompt_fragments": owner._get_tui_prompt_fragments(),
                    "flags": {name: (value if value is None or isinstance(value, bool) else
                                      {"type": type(value).__name__, "truthy": bool(value)})
                              for name in names for value in (getattr(owner, name, None),)},
                    "flags_truthy": {name: bool(getattr(owner, name, None)) for name in names},
                    "app_is_running": app.is_running,
                }
                state["finished_monotonic_ns"] = time.monotonic_ns()
                target = Path(output).with_name(f"snapshot-{spec['id']}.json")
                staged = target.with_suffix(".pending")
                staged.write_text(json.dumps(state, indent=2, default=repr) + "\n", encoding="utf-8")
                staged.replace(target)
        except Exception as error:
            # Never let an observer failure invoke the terminal-owning exception
            # handler. A missing error record is itself inconclusive, not success.
            try:
                request.unlink(missing_ok=True)
                target = Path(output).with_name("snapshot-error.json")
                staged = target.with_suffix(".pending")
                staged.write_text(json.dumps({
                    "status": "error", "error_type": type(error).__name__, "request": spec,
                    "monotonic_ns": time.monotonic_ns(), "wall_time_ns": time.time_ns(),
                    "interpretation": "inconclusive",
                }, default=repr) + "\n", encoding="utf-8")
                staged.replace(target)
            except Exception:
                pass
        await asyncio.sleep(0.02)
