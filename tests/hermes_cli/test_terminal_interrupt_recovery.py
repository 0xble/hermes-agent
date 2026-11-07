"""Regression tests for #33271: terminal recovery after interrupt.

When the user interrupts a running agent turn by typing a new message,
prompt_toolkit may have an in-flight ``CSI 6n`` cursor-position query whose
reply (``ESC[<row>;<col>R``) arrives on stdin after the input parser has torn
down. The reply then leaks as literal text (``^[[19;1R``) and the VT100 parser
can stall, accepting no further keystrokes — the terminal appears frozen.

The recovery path lives in ``HermesCLI._recover_terminal_after_interrupt()``,
which is invoked from ``process_loop``'s ``finally`` block only when
``self._last_turn_interrupted`` is set. It must:
  1. Drain stray escape bytes from the OS input buffer (``flush_stdin``).
  2. Force a clean prompt_toolkit renderer redraw (``_force_full_redraw``).

These tests exercise the real method (not a re-implementation of its logic).
"""

import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from cli import HermesCLI

@pytest.fixture
def bare_cli():
    """A HermesCLI with no __init__ — we only exercise the recovery helper."""
    return object.__new__(HermesCLI)

class TestRecoverTerminalAfterInterrupt:
    """Directly exercise HermesCLI._recover_terminal_after_interrupt()."""

    def test_redraw_still_runs_when_flush_fails(self, bare_cli):
        """A flush_stdin failure (no TTY, non-POSIX) must not skip the redraw.

        The two recovery steps are independent — losing the stdin drain must
        never leave the renderer un-repainted.
        """
        bare_cli._force_full_redraw = MagicMock()
        with patch(
            "hermes_cli.curses_ui.flush_stdin", side_effect=OSError("no tty")
        ):
            bare_cli._recover_terminal_after_interrupt()  # must not raise

        bare_cli._force_full_redraw.assert_called_once()

    def test_flush_runs_before_redraw(self, bare_cli):
        """Order matters: drain stray bytes first so they don't arrive mid-redraw."""
        events = []
        bare_cli._force_full_redraw = MagicMock(
            side_effect=lambda: events.append("redraw")
        )
        with patch(
            "hermes_cli.curses_ui.flush_stdin",
            side_effect=lambda: events.append("flush"),
        ):
            bare_cli._recover_terminal_after_interrupt()

        assert events == ["flush", "redraw"]

    def test_flush_stdin_is_tty_gated(self):
        """The real flush_stdin is a no-op on non-TTY stdin (piped/redirected).

        Under pytest stdin is not a TTY, so this must return cleanly without
        touching termios.
        """
        from hermes_cli.curses_ui import flush_stdin

        flush_stdin()  # must not raise in a non-TTY test environment


class _ThreadRecordingStdout:
    """Terminal stand-in for a real Vt100_Output: records which thread wrote each chunk."""

    def __init__(self, events):
        self.events = events
        self.recording = False
        self.encoding = "utf-8"

    def write(self, data):
        if self.recording and data:
            self.events.append(("write", threading.get_ident(), threading.current_thread().name, len(data)))
        return len(data)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("not a real terminal")


class TestInterruptRecoveryThreadAffinity:
    """The interrupt recovery runs on the input worker thread (``_tui_process_loop`` →
    ``_tui_after_turn``). prompt_toolkit's renderer and output belong to the app's event loop,
    so every renderer reset/erase and terminal write that recovery causes must happen there,
    exactly once, never on the worker. Real Application, real running loop, real worker, real
    ``_recover_terminal_after_interrupt``/``_force_full_redraw``; only observation is added."""

    def test_recovery_mutates_renderer_and_output_only_on_the_app_loop(self, bare_cli, tmp_path):
        import cli as cli_mod
        from prompt_toolkit.application import Application
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.layout import Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.output.vt100 import Vt100_Output

        events: list = []
        stdout = _ThreadRecordingStdout(events)
        output = Vt100_Output(stdout, lambda: Size(rows=24, columns=80), term="xterm", enable_cpr=False)
        identity = {"cli_module": cli_mod.__file__, "thread_of": {}}

        with create_pipe_input() as pipe_input:
            app = Application(layout=Layout(Window(FormattedTextControl("\u276f "))),
                              input=pipe_input, output=output)
            renderer = app.renderer
            for name in ("reset", "erase"):
                original = getattr(renderer, name)

                def observed(*args, _original=original, _name=name, **kwargs):
                    if stdout.recording:
                        events.append((_name, threading.get_ident(), threading.current_thread().name,
                                       kwargs.get("leave_alternate_screen", args[0] if args else None)))
                    return _original(*args, **kwargs)
                setattr(renderer, name, observed)

            bare_cli._app = app
            bare_cli._terminal_io_broken = False
            worker_errors: list = []

            def worker():
                try:
                    bare_cli._recover_terminal_after_interrupt()
                except BaseException as exc:  # recorded, asserted below
                    worker_errors.append(repr(exc))

            rendered = asyncio.Event()
            app.after_render += lambda _app: rendered.set()
            owned: dict = {}

            async def drive():
                identity["thread_of"]["loop"] = threading.get_ident()
                loop = asyncio.get_running_loop()
                # Ready only once the app has really painted (after_render), not after a delay.
                await asyncio.wait_for(rendered.wait(), 5)
                rendered.clear()
                stdout.recording = True
                thread = owned["worker"] = threading.Thread(target=worker, name="tui-process-loop", daemon=True)
                thread.start()
                identity["thread_of"]["worker"] = thread.ident
                await asyncio.to_thread(thread.join, 10)
                identity["worker_alive_after_join"] = thread.is_alive()
                # call_soon is FIFO: this marker runs only after anything the worker queued.
                drained = loop.create_future()
                loop.call_soon(drained.set_result, None)
                await drained
                # The recovery's invalidate yields one real repaint; wait for it explicitly.
                await asyncio.wait_for(rendered.wait(), 5)

            async def main():
                # App and driver share one lifetime: whichever ends first, the other is ended
                # here, so a driver failure surfaces at once instead of at the file timeout.
                app_task = asyncio.ensure_future(app.run_async())
                driver = asyncio.ensure_future(drive())
                try:
                    await asyncio.wait({app_task, driver}, return_when=asyncio.FIRST_COMPLETED)
                    if not driver.done():
                        app_task.result()  # the app's own failure, if it had one
                        raise AssertionError("app exited before the recovery driver finished")
                    driver.result()  # the driver's original failure, if it had one
                finally:
                    stdout.recording = False
                    if not driver.done():
                        driver.cancel()
                    if not app_task.done():
                        try:
                            app.exit()
                        except Exception:
                            app_task.cancel()
                    await asyncio.gather(driver, app_task, return_exceptions=True)

            try:
                asyncio.run(main())
            finally:
                if "worker" in owned:
                    owned["worker"].join(10)  # same bound as the driver's join; never extended

        loop_id = identity["thread_of"]["loop"]
        worker_id = identity["thread_of"]["worker"]
        evidence = {
            **identity,
            "worker_errors": worker_errors,
            "events": [{"kind": kind, "thread": "loop" if tid == loop_id else "worker" if tid == worker_id else name,
                        "detail": detail} for kind, tid, name, detail in events],
        }
        (tmp_path / "thread-affinity.json").write_text(json.dumps(evidence, indent=2))
        print("THREAD-AFFINITY-EVIDENCE " + json.dumps(evidence))

        assert not worker_errors and not identity["worker_alive_after_join"], evidence
        recovery_resets = [e for e in evidence["events"] if e["kind"] == "reset" and e["detail"] is False]
        assert len(recovery_resets) == 1, f"recovery reset count {len(recovery_resets)}: {evidence}"
        off_loop = [e for e in evidence["events"] if e["thread"] != "loop"]
        assert not off_loop, f"renderer/output mutated off the app loop: {off_loop}"

    def test_redraw_on_the_app_loop_stays_synchronous(self, bare_cli):
        """Ctrl+L and /redraw run on the loop: the repaint happens before the call returns."""
        app = MagicMock()
        bare_cli._app = app
        bare_cli._terminal_io_broken = False
        bare_cli._redraw_rebuilds_scrollback = lambda: True

        async def on_loop():
            app.loop = asyncio.get_running_loop()
            bare_cli._force_full_redraw()
            return app.renderer.output.erase_screen.call_count, app.invalidate.called

        assert asyncio.run(on_loop()) == (1, True)

    def test_redraw_without_a_running_loop_stays_synchronous(self, bare_cli):
        app = MagicMock()
        app.loop = asyncio.new_event_loop()
        try:
            bare_cli._app = app
            bare_cli._terminal_io_broken = False
            bare_cli._redraw_rebuilds_scrollback = lambda: True
            bare_cli._force_full_redraw()
        finally:
            app.loop.close()
        app.renderer.output.erase_screen.assert_called_once()
        app.invalidate.assert_called()

    @pytest.mark.parametrize("change", ["broken", "app_replaced"])
    def test_queued_redraw_rechecks_state_when_it_runs(self, bare_cli, change):
        """A repaint queued from the worker must not paint a PTY that broke, or an app that was
        replaced, before the loop got to it (#81521)."""
        loop = asyncio.new_event_loop()
        started, release = threading.Event(), threading.Event()
        runner = threading.Thread(target=loop.run_forever, name="app-loop", daemon=True)
        runner.start()
        try:
            def hold():
                started.set()
                release.wait(5)
            loop.call_soon_threadsafe(hold)
            assert started.wait(5)
            app = MagicMock()
            app.loop = loop
            bare_cli._app = app
            bare_cli._terminal_io_broken = False
            bare_cli._redraw_rebuilds_scrollback = lambda: True

            bare_cli._force_full_redraw()  # off-loop: queued behind hold()
            if change == "broken":
                bare_cli._terminal_io_broken = True
            else:
                bare_cli._app = MagicMock()
            release.set()

            async def marker():
                return None
            asyncio.run_coroutine_threadsafe(marker(), loop).result(5)
        finally:
            release.set()
            loop.call_soon_threadsafe(loop.stop)
            runner.join(5)
            loop.close()
        app.renderer.output.erase_screen.assert_not_called()
        app.renderer.reset.assert_not_called()
        app.invalidate.assert_not_called()
