"""File-backed supervision for explicitly restart-surviving local background jobs."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession

logger = logging.getLogger(__name__)


class ProcessSurvivalMixin:
    """Keep restart survival separate from ordinary pipe/PTY process supervision."""

    def _spawn_local_survivor(self, session: ProcessSession, safe_command: str, env_vars: dict) -> ProcessSession:
        """Launch an explicitly restart-surviving local command without a gateway pipe."""
        from tools import process_registry as registry

        if registry._IS_WINDOWS:
            raise ValueError("survive_restart is not supported on Windows")
        run_dir = registry.get_hermes_home() / "process-runs" / session.id
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        session.survive_restart = True
        session.log_path = str(run_dir / "out.log")
        session.exit_path = str(run_dir / "exit")
        session.delivered_path = str(run_dir / "delivered")
        q = shlex.quote
        wrapped = (
            f"( {safe_command} ) > {q(session.log_path)} 2>&1; "
            f"rc=$?; printf '%s\\n' \"$rc\" > {q(session.exit_path)}"
        )
        command_argv = [registry._find_shell(), "-lic", f"set +m; {wrapped}"]
        dispatch = registry.restart_safe_gateway_child_argv(
            command_argv,
            unit_suffix=session.id,
            require_restart_safe_scope=True,
            outlives_parent=False,
        )
        if dispatch.mode == "scoped":
            session.systemd_unit = f"hermes-worker-{session.id}.scope"
        spawn_env = self._spawn_env(env_vars)
        if dispatch.mode == "scoped":
            spawn_env = registry.systemd_user_bus_env(spawn_env)
        proc = subprocess.Popen(
            dispatch.argv,
            text=True,
            cwd=session.cwd,
            env=spawn_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        session.process = proc
        session.pid = proc.pid
        session.host_start_time = self._safe_host_start_time(session.pid)
        try:
            self._track_started(session, self._survivor_poller_loop, f"proc-survivor-poller-{session.id}")
        except Exception:
            self._reap_untracked(session, proc)
            raise
        return session

    def stop_survivor_pollers(self) -> None:
        """Fence survivor pollers before gateway shutdown drains in-memory notifications."""
        stopped = getattr(self, "_survivor_pollers_stopped", None)
        fence = getattr(self, "_survivor_poller_fence", None)
        if stopped is not None:
            if fence is None:
                stopped.set()
            else:
                with fence:
                    stopped.set()

    def _finish_survivor_if_allowed(self, session: ProcessSession, exit_code, reason: str, source: str) -> bool:
        """Finish only while the shutdown fence is open; return whether delivery was queued."""
        stopped = getattr(self, "_survivor_pollers_stopped", None)
        fence = getattr(self, "_survivor_poller_fence", None)
        if fence is None:
            if stopped is not None and stopped.is_set():
                return False
            self._finish_survivor(session, exit_code, reason, source)
            return True
        with fence:
            if stopped is not None and stopped.is_set():
                return False
            self._finish_survivor(session, exit_code, reason, source)
            return True

    def _survivor_poller_loop(self, session: ProcessSession):
        """Collect a file-backed local survivor after this or a successor gateway boots."""
        if getattr(getattr(self, "_survivor_pollers_stopped", None), "is_set", lambda: False)():
            return
        exit_path = Path(session.exit_path)
        log_path = Path(session.log_path)
        while not session.exited:
            if getattr(getattr(self, "_survivor_pollers_stopped", None), "is_set", lambda: False)():
                return
            if session._kill_requested:
                time.sleep(0.05)
                continue
            try:
                exit_text = ""
                if exit_path.exists():
                    exit_text = exit_path.read_text(encoding="utf-8", errors="replace").strip()
                if exit_text:
                    try:
                        exit_code = int(exit_text.splitlines()[-1])
                    except ValueError:
                        exit_code = -1
                    if not self._finish_survivor_if_allowed(session, exit_code, "exited", ""):
                        return
                    return
                alive = (
                    session.process.poll() is None
                    if session.process is not None
                    else self._detached_host_fate(session.pid, session.host_start_time) == "running"
                )
                if not alive:
                    # Give the wrapper's final printf a short chance to become visible before
                    # reporting a genuinely lost process.
                    time.sleep(0.05)
                    if not exit_path.exists() or not exit_path.read_text(encoding="utf-8", errors="replace").strip():
                        if not self._finish_survivor_if_allowed(session, None, "lost", "gateway_shutdown"):
                            return
                        return
                if log_path.exists():
                    output = log_path.read_text(encoding="utf-8", errors="replace")
                    with session._lock:
                        session.output_buffer = output[-session.max_output_chars:]
                        session.total_output_chars = len(output)
            except OSError:
                self._finish_survivor_if_allowed(session, None, "lost", "gateway_shutdown")
                return
            time.sleep(0.1)

    def _finish_survivor(self, session: ProcessSession, exit_code, reason: str, source: str) -> None:
        try:
            output = Path(session.log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        with session._lock:
            session.output_buffer = output[-session.max_output_chars:]
            session.total_output_chars = len(output)
        self._finish_exited(session, exit_code, reason=reason, source=source)

    def _claim_survivor_delivery(self, session: ProcessSession) -> bool:
        """Claim one durable completion notification across gateway boots."""
        if not session.delivered_path:
            return True
        try:
            path = Path(session.delivered_path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as marker:
                json.dump({"delivered": True}, marker)
            return True
        except FileExistsError:
            return False
        except OSError:
            logger.warning("Could not claim survivor completion %s", session.id, exc_info=True)
            return True
