"""Topical phases split from tools/process_registry.py; original module bindings remain late-bound."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from logging import Logger

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession

class ProcessRegistry:
    def _finish_reader(self, session, decoder, append, label, wait, exit_code) -> None:
        """Reader-thread teardown: flush the decoder (a truncated multibyte tail becomes
        one U+FFFD instead of vanishing), reap the child (no zombies), record the exit.

        A process may close stdout long before it exits.  The reader owns a dedicated
        daemon thread, so it must keep waiting rather than publish a false completion
        and discard the only ``Popen`` handle that can reap the child.
        """
        from tools.process_registry import suppress, logger
        with suppress(Exception):
            tail = decoder.decode(b"", final=True)
            if tail:
                append(tail)
        try:
            wait()
        except Exception as e:
            # A PTY child reaped by isalive() already has its exitstatus; only an
            # unknown status must stay tracked for later reconciliation.
            if exit_code() is None:
                logger.warning("%s wait failed; leaving process tracked: %s", label, e)
                return
            logger.warning("%s wait failed; recording known exit status: %s", label, e)
        self._finish_exited(session, exit_code())


    def _env_poller_loop(self, session: ProcessSession, env: Any, log_path: str, pid_path: str, exit_path: str):
        from tools.process_registry import logger
        return _poll_environment(self, session, env, log_path, pid_path, exit_path, logger)


def _poll_environment(self, session: ProcessSession, env: Any, log_path: str, pid_path: str, exit_path: str, logger: Logger):
    """Background thread: poll a sandbox log file for non-local backends.

    One ``execute()`` per tick: the read command also reports the process
    status (see ``log_delta_command``). Sandboxes rate-limit exec starts."""
    from tools.process_registry import shlex, time, parse_log_delta_reply, fallback_exit_code
    q = shlex.quote
    prev_output_bytes = 0
    while not session.exited:
        time.sleep(2)
        try:
            raw = env.execute(
                self._log_delta_command(q(log_path), prev_output_bytes, q(pid_path), q(exit_path)),
                timeout=10).get("output", "")
            reply = parse_log_delta_reply(raw)
            delta = reply.delta if reply else ""
            if reply:
                if reply.offset < prev_output_bytes:
                    with session._lock:
                        if session.exited:
                            return
                        session.output_buffer = ""
                prev_output_bytes = reply.size
            if delta:
                with session._lock:
                    if session.exited:
                        return
                    session.output_buffer += delta
                    if len(session.output_buffer) > session.max_output_chars:
                        session.output_buffer = session.output_buffer[-session.max_output_chars:]
                self._check_watch_patterns(session, delta)
                self._emit_output(session, delta)
            exit_code = reply.exit_code if reply else fallback_exit_code(env, q(pid_path), q(exit_path))
            if exit_code is not None:
                self._finish_exited(session, exit_code)
                return
        except Exception as e:
            self._finish_exited(session, -1, reason="lost", source="backend_lost")
            logger.error("environment process poll failed for %s: %s", session.id, e, exc_info=True)
            return

