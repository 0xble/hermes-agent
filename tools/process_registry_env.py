"""Legacy sandbox spawning/polling and the named-node SSH dispatch seam."""
from __future__ import annotations

from contextlib import suppress
import shlex
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession


class EnvironmentProcessMixin:
    def _kill_nonhost_targets(self, targets, kill_one) -> int:
        if any(session.pid_scope == "ssh" for session in targets):
            from concurrent.futures import ThreadPoolExecutor
            from tools.process_registry import _SWEEP_MAX_WORKERS
            with ThreadPoolExecutor(max_workers=min(_SWEEP_MAX_WORKERS, len(targets)), thread_name_prefix="process-kill") as pool:
                return sum(bool(future.result()) for future in [pool.submit(kill_one, s) for s in targets])
        # Opaque registry-only records retain the legacy serial kill contract.
        return sum(bool(kill_one(session)) for session in targets)

    def _signal_environment_group(self, session, sig, deadline=None) -> bool:
        if session.pid_scope == "ssh":
            return self._kill_ssh(session, deadline=deadline).get("status") in {"killed", "already_exited"}
        if not session.env_ref or not session.pid:
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if remaining is not None and remaining <= 0:
            return False
        with suppress(Exception):
            session.env_ref.execute(f"kill -{int(sig)} {session.pid} 2>/dev/null",
                                    timeout=remaining if remaining is not None else 5)
            return True
        return False

    def spawn_via_env(
        self, env: Any, command: str, cwd: str | None = None, task_id: str = "", session_key: str = "",
        timeout: int = 10, owner_task_id: str = "", persist_on_release: bool = False) -> ProcessSession:
        """Spawn a background process inside a non-local backend's sandbox.
        The command is wrapped to capture its in-sandbox PID and redirect output to a
        log file that later execute() calls poll. No live pipe or stdin, but it runs in
        the correct sandbox context. ``persist_on_release`` keeps the process out of
        agent-lifecycle kill sweeps (#41225)."""
        from tools.nodes import Node
        node = getattr(env, "_hermes_node", None)
        if isinstance(node, Node):
            return self._spawn_ssh(env, node, command, cwd, task_id, owner_task_id, session_key,
                                   timeout, persist_on_release)
        session = self._new_session(command, task_id, owner_task_id, session_key, cwd, env_ref=env, pid_scope="sandbox",
                                    persist_on_release=persist_on_release)
        temp_dir = self._env_temp_dir(env)
        log_path, pid_path, exit_path = (f"{temp_dir}/hermes_bg_{session.id}.{ext}" for ext in ("log", "pid", "exit"))
        q = shlex.quote
        bg_command = (
            f"mkdir -p {q(temp_dir)} && "
            f"( nohup bash -lc {q(command)} > {q(log_path)} 2>&1; "
            f"rc=$?; printf '%s\\n' \"$rc\" > {q(exit_path)} ) & "
            f"echo $! > {q(pid_path)} && cat {q(pid_path)}")
        result = env.execute(bg_command, cwd=cwd or "", timeout=timeout, rewrite_compound_background=False)
        output = result.get("output", "").strip()
        session.pid = next((int(ln) for ln in map(str.strip, output.splitlines()) if ln.isdigit()), None)
        # No PID from the wrapper (no bash in the image, sandbox gone, broken redirect): the
        # command never started, so raise like a failed Popen instead of returning a session.
        if session.pid is None:
            raise RuntimeError(f"launch wrapper exited {result.get('returncode')} without a PID: "
                               f"{output or '(no output)'}")
        self._track_started(
            session, self._env_poller_loop, f"proc-poller-{session.id}", (env, log_path, pid_path, exit_path))
        return session

    def _env_poller_loop(self, session: ProcessSession, env: Any, log_path: str, pid_path: str, exit_path: str):
        """Background thread: poll a sandbox log file for non-local backends."""
        q = shlex.quote
        # Byte offset already read from the log (bytes, not chars: the shell counts bytes).
        prev_output_bytes = 0
        while not session.exited:
            time.sleep(2)
            try:
                # Read only the bytes written since the last poll.
                raw = env.execute(self._log_delta_command(q(log_path), prev_output_bytes),
                                  timeout=10).get("output", "")
                header, _, delta = raw.partition("\n")
                try:
                    size_str, offset_str = header.split()
                    new_size = int(size_str)
                    used_offset = int(offset_str)
                except ValueError:
                    # No usable header (command failed, shell missing a tool): skip this
                    # poll rather than act on a half-read value.
                    new_size = None
                    used_offset = None
                    delta = ""
                if new_size is not None:
                    if used_offset < prev_output_bytes:
                        # Log rotated/truncated: what we hold no longer lines up. Restart.
                        with session._lock:
                            if session.exited:
                                return
                            session.output_buffer = ""
                    prev_output_bytes = new_size
                if delta:
                    # A kill that landed after this fetch already published its output
                    # snapshot: leave the output as the kill reported it (as the PTY reader does).
                    with session._lock:
                        if session.exited:
                            return
                        session.output_buffer += delta
                        if len(session.output_buffer) > session.max_output_chars:
                            session.output_buffer = session.output_buffer[-session.max_output_chars:]
                    self._check_watch_patterns(session, delta)
                    self._emit_output(session, delta)

                check = env.execute(
                    f"kill -0 \"$(cat {q(pid_path)} 2>/dev/null)\" 2>/dev/null; echo $?", timeout=5)
                check_output = check.get("output", "").strip()
                if check_output and check_output.splitlines()[-1].strip() != "0":
                    # Exited -- read the exit code captured by the wrapper shell.
                    exit_str = env.execute(f"cat {q(exit_path)} 2>/dev/null", timeout=5).get("output", "").strip()
                    try:
                        exit_code = int(exit_str.splitlines()[-1].strip())
                    except (ValueError, IndexError):
                        exit_code = -1
                    self._finish_exited(session, exit_code)
                    return
            except Exception:
                # Environment might be gone (sandbox reaped, etc.). A kill that already
                # settled the session keeps its result.
                self._finish_exited(session, -1, reason="lost", source="backend_lost")
                return

