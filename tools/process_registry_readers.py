"""Topical phases split from tools/process_registry.py; original module bindings remain late-bound."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from tools.process_registry import ProcessRegistry as ConcreteProcessRegistry, ProcessSession

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


    def get(self, session_id: str):
        """Resolve a handle and refresh its original backend only."""
        return self._refresh_detached_session(self._lookup_session(session_id))

    def _env_poller_loop(self, session: ProcessSession, env: Any,
                         log_path: str, pid_path: str, exit_path: str):
        """Background thread: poll a sandbox log file for non-local backends.

        One ``execute()`` per tick: the read command also reports the process
        status (see ``log_delta_command``). Sandboxes rate-limit exec starts."""
        from tools.process_registry import shlex, time, parse_log_delta_reply, fallback_exit_code
        registry = cast("ConcreteProcessRegistry", self)
        q = shlex.quote
        # Byte offset already read from the log (bytes, not chars: the shell counts bytes).
        prev_output_bytes = 0
        while not session.exited:
            time.sleep(2)
            try:
                # Read only the bytes written since the last poll, plus the process status.
                raw = env.execute(
                    registry._log_delta_command(q(log_path), prev_output_bytes, q(pid_path), q(exit_path)),
                    timeout=10).get("output", "")
                reply = parse_log_delta_reply(raw)
                delta = reply.delta if reply else ""
                if reply:
                    if reply.offset < prev_output_bytes:
                        # Log rotated/truncated: what we hold no longer lines up. Restart.
                        with session._lock:
                            if session.exited:
                                return
                            session.output_buffer = ""
                    prev_output_bytes = reply.size
                if delta:
                    # A kill that landed after this fetch already published its output
                    # snapshot: leave the output as the kill reported it (as the PTY reader does).
                    with session._lock:
                        if session.exited:
                            return
                        session.output_buffer += delta
                        if len(session.output_buffer) > session.max_output_chars:
                            session.output_buffer = session.output_buffer[-session.max_output_chars:]
                    registry._check_watch_patterns(session, delta)
                    registry._emit_output(session, delta)

                # Unreadable reply: separate liveness and exit-code reads, as before the combined read.
                exit_code = reply.exit_code if reply else fallback_exit_code(env, q(pid_path), q(exit_path))
                if exit_code is not None:
                    registry._finish_exited(session, exit_code)
                    return
            except Exception:  # health: allow BLE001 -- any backend transport failure settles the dead poller without logging command data
                # Environment might be gone (sandbox reaped, etc.). A kill that already
                # settled the session keeps its result.
                registry._finish_exited(session, -1, reason="lost", source="backend_lost")
                return

    def _release_finished_handles(self, session: ProcessSession):
        """Close a finished session's OS handles (Popen pipes / PTY master).

        Best-effort and idempotent: the session may have no local Popen (env
        backends, detached recovery), or the handles may already be closed by
        the reader loop / kill path. Closing a Popen's stream objects does not
        kill anything — the child has already exited — it only releases the
        parent's pipe FDs, which is exactly the retained-resource leak.
        """
        from tools.process_registry import _IS_WINDOWS, suppress, threading
        proc = session.process
        if proc is not None:
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None:
                    with suppress(OSError, ValueError):  # a stdin flush can hit EPIPE
                        stream.close()
        if session._pty is not None:
            # A live ptyprocess reader sits in a blocking read holding the PTY file
            # object's buffer lock, and that read only ends once every holder of
            # the slave side is gone. A descendant that setsid()s past the kill
            # keeps it open, so close() here would block forever (under _lock on
            # the prune path). The reader closes the PTY itself via
            # _finish_reader once its read ends. pywinpty reads don't block, so
            # Windows closes here as before.
            reader = session._reader_thread
            if (not _IS_WINDOWS and reader is not None and reader.is_alive()
                    and reader is not threading.current_thread()):
                return
            # ptyprocess/pywinpty close() is idempotent (``closed`` flag) and
            # closes the master fd exactly once; it raises only if the child
            # ignores SIGKILL, which we don't want to surface on the finish path.
            with suppress(Exception):
                session._pty.close()

