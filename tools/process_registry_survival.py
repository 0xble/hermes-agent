"""File-backed supervision for explicitly restart-surviving local background jobs."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
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
            f"(\n{safe_command}\n) > {q(session.log_path)} 2>&1\n"
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
        try:
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
        except Exception:
            self._remove_survivor_run_dir(session)
            raise
        session.process = proc
        session.pid = proc.pid
        session.host_start_time = self._safe_host_start_time(session.pid)
        try:
            self._track_started(session, self._survivor_poller_loop, f"proc-survivor-poller-{session.id}")
        except Exception:
            self._reap_untracked(session, proc)
            self._remove_survivor_run_dir(session)
            raise
        return session

    @staticmethod
    def _survivor_run_dir(session: ProcessSession) -> Path | None:
        return Path(session.log_path).parent if session.log_path else None

    def _remove_survivor_run_dir(self, session: ProcessSession) -> None:
        """Remove only this session's profile-local survivor directory."""
        run_dir = self._survivor_run_dir(session)
        if run_dir is None:
            return
        try:
            from tools import process_registry as registry
            expected_parent = registry.get_hermes_home() / "process-runs"
            if run_dir.parent.resolve() != expected_parent.resolve() or run_dir.name != session.id:
                return
            shutil.rmtree(run_dir, ignore_errors=True)
        except OSError:
            logger.debug("Could not prune survivor run directory %s", run_dir, exc_info=True)

    def _survivor_delivery_state(self, session: ProcessSession) -> str | None:
        if not session.delivered_path:
            return None
        try:
            payload = json.loads(Path(session.delivered_path).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return "claimed"
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            return "claimed"
        if payload.get("delivered") is True or payload.get("state") == "delivered":
            return "delivered"
        return "claimed"

    def _claim_survivor_delivery(self, session: ProcessSession) -> bool:
        """A queued event is not delivered: a successor may replay it after a crash."""
        return self._survivor_delivery_state(session) != "delivered"

    def _mark_survivor_delivered(self, session: ProcessSession) -> bool:
        """Acknowledge consumption/admission; exclusively dedup subsequent drains."""
        if not session.delivered_path:
            return True
        try:
            import fcntl
            from utils import atomic_json_write
            path = Path(session.delivered_path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path.with_name("delivery.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                if self._survivor_delivery_state(session) == "delivered":
                    return False
                # A crash may leave an unacknowledged claim, never a partial delivered marker.
                atomic_json_write(path, {"state": "delivered", "delivered": True}, mode=0o600, fsync_dir=True)
            self._write_checkpoint()
            return True
        except OSError:
            logger.warning("Could not record delivered survivor completion %s", session.id, exc_info=True)
            return True

    def acknowledge_survivor_completion(self, session_id: str) -> None:
        session = self.get(session_id)
        if session is not None and session.survive_restart:
            self._mark_survivor_delivered(session)

    def _read_survivor_log(self, session: ProcessSession) -> None:
        """Append only bytes not read on the previous poll, bounded on recovery."""
        if not session.log_path:
            return
        log_path = Path(session.log_path)
        try:
            size = log_path.stat().st_size
            offset = getattr(session, "_survivor_log_offset", None)
            if offset is None:
                offset = max(0, size - session.max_output_chars)
            if size < offset:
                offset = 0
                with session._lock:
                    session.output_buffer = ""
            offset = max(offset, size - session.max_output_chars)
            if size == offset:
                session._survivor_log_offset = offset
                return
            with log_path.open("rb") as log_file:
                log_file.seek(offset)
                chunk = log_file.read(session.max_output_chars)
            session._survivor_log_offset = offset + len(chunk)
            text = chunk.decode("utf-8", errors="replace")
            with session._lock:
                session.output_buffer += text
                session.total_output_chars += len(text)
                if len(session.output_buffer) > session.max_output_chars:
                    session.output_buffer = session.output_buffer[-session.max_output_chars:]
        except OSError:
            return

    def prune_survivor_run_dir(self, session: ProcessSession) -> None:
        """Prune an aged survivor only after its completion was delivered."""
        if session.survive_restart and self._survivor_delivery_state(session) == "delivered":
            self._remove_survivor_run_dir(session)

    def _prune_delivered_survivor_runs(self) -> None:
        """Bound delivered artifacts even if their registry died before TTL pruning."""
        from tools import process_registry as registry
        run_root = registry.get_hermes_home() / "process-runs"
        now = time.time()
        tracked = self._running.keys() | self._finished.keys()
        try:
            for run_dir in run_root.iterdir():
                if (run_dir.is_symlink() or not run_dir.is_dir()
                        or not run_dir.name.startswith("proc_") or run_dir.name in tracked):
                    continue
                marker = run_dir / "delivered"
                try:
                    if now - marker.stat().st_mtime <= registry.FINISHED_TTL_SECONDS:
                        continue
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    if isinstance(payload, dict) and payload.get("delivered") is True:
                        shutil.rmtree(run_dir)
                except (OSError, ValueError, TypeError):
                    continue
        except OSError:
            return

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
                self._read_survivor_log(session)
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
                    exit_text = exit_path.read_text(encoding="utf-8", errors="replace").strip() if exit_path.exists() else ""
                    if not exit_text:
                        if not self._finish_survivor_if_allowed(session, None, "lost", "survivor_lost"):
                            return
                        return
            except OSError:
                self._finish_survivor_if_allowed(session, None, "lost", "survivor_lost")
                return
            time.sleep(0.1)

    def _finish_survivor(self, session: ProcessSession, exit_code, reason: str, source: str) -> None:
        self._read_survivor_log(session)
        self._finish_exited(session, exit_code, reason=reason, source=source)
