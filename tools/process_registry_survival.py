"""File-backed local supervision with opt-in restart-safe dispatch and delivery."""

from __future__ import annotations

import codecs
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

# Claim nonces of registries alive in this process image. An exec'd predecessor keeps
# the PID and start time but not this set, so its unacknowledged claims are stale.
_LIVE_CLAIM_NONCES: set[str] = set()


def is_restart_safe(entry_or_session) -> bool:
    """Only an explicit opt-in grants restart-safe lifecycle and delivery semantics."""
    if isinstance(entry_or_session, dict):
        return bool(entry_or_session.get("survive_restart", False))
    return bool(getattr(entry_or_session, "survive_restart", False))


def has_exit_marker(entry_or_session) -> bool:
    """An exit receipt is a collection capability, not a restart-survival claim."""
    if isinstance(entry_or_session, dict):
        return bool(entry_or_session.get("exit_path"))
    return bool(getattr(entry_or_session, "exit_path", ""))


class ProcessSurvivalMixin:
    """Share durable I/O without granting ordinary jobs restart-safe lifecycle semantics."""

    def _spawn_local_survivor(
        self, session: ProcessSession, safe_command: str, env_vars: dict | None, *, restart_safe: bool = True,
        unit_suffix: str | None = None,
    ) -> ProcessSession:
        """Launch a local command whose stdout/stderr never pass through a gateway pipe.

        ``restart_safe`` (survive_restart) also claims restart survival: a restart-safe
        dispatch and exactly-once delivery. ``restart_safe=False`` (persist_on_release)
        keeps the ordinary dispatch and delivery and borrows only the file-backed I/O, so
        a job outliving its gateway never writes into that gateway's closed pipe."""
        from tools import process_registry as registry

        if registry._IS_WINDOWS:
            raise ValueError("survive_restart is not supported on Windows")
        run_dir = registry.get_hermes_home() / "process-runs" / session.id
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        session.survive_restart = restart_safe
        session.log_path = str(run_dir / "out.log")
        session.exit_path = str(run_dir / "exit")
        session.delivered_path = str(run_dir / "delivered") if is_restart_safe(session) else ""
        wrapped = self._local_exit_wrapper(session, safe_command)
        if is_restart_safe(session):
            command_argv = [registry._find_shell(), "-lic", f"set +m; {wrapped}"]
            dispatch = registry.restart_safe_gateway_child_argv(
                command_argv,
                unit_suffix=session.id,
                require_restart_safe_scope=True,
                outlives_parent=False,
            )
            if dispatch.mode == "scoped":
                session.systemd_unit = f"hermes-worker-{session.id}.scope"
            spawn_argv, scoped = dispatch.argv, dispatch.mode == "scoped"
        else:
            spawn_argv = self._scope_argv(session, wrapped, unit_suffix or session.id, "Local")
            scoped = bool(session.systemd_unit)
        spawn_env = self._spawn_env(env_vars)
        if scoped:
            spawn_env = registry.systemd_user_bus_env(spawn_env)
        try:
            # Parser errors precede the inner redirection. Keep the outer shell's
            # diagnostics in the same append-only durable log as command output.
            with open(session.log_path, "ab") as outer_stderr:
                proc = subprocess.Popen(
                    spawn_argv,
                    text=True,
                    cwd=session.cwd,
                    env=spawn_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=outer_stderr,
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

    def _local_exit_wrapper(self, session: ProcessSession, safe_command: str) -> str:
        """Publish an outliving job's status atomically, independently of its reader."""
        q = shlex.quote
        # EXIT honors inherited errexit. Only the private temp marker is forcibly
        # overwritten under noclobber; rename publishes it. bash exposes 258 to
        # the trap on parser failure but its real exit is 2.
        exit_trap = (
            f"rc=$(($? & 255)); printf '%s\\n' \"$rc\" >| {q(session.exit_path + '.tmp')}; "
            f"mv -f -- {q(session.exit_path + '.tmp')} {q(session.exit_path)}"
        )
        return f"trap {q(exit_trap)} EXIT\n(\n{safe_command}\n) >> {q(session.log_path)} 2>&1"

    @staticmethod
    def _survivor_run_dir(session: ProcessSession) -> Path | None:
        path = session.exit_path or session.log_path
        return Path(path).parent if path else None

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

    @staticmethod
    def _read_survivor_marker(session: ProcessSession) -> dict | None:
        """The delivery marker: absent (pending), a claim with its owner, or delivered."""
        try:
            payload = json.loads(Path(session.delivered_path).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            return {"state": "claimed"}
        return payload if isinstance(payload, dict) else {"state": "claimed"}

    @staticmethod
    def _marker_delivered(payload: dict | None) -> bool:
        return payload is not None and (payload.get("delivered") is True or payload.get("state") == "delivered")

    def _survivor_delivery_state(self, session: ProcessSession) -> str | None:
        if not session.delivered_path:
            return None
        payload = self._read_survivor_marker(session)
        if payload is None:
            return None
        return "delivered" if self._marker_delivered(payload) else "claimed"

    def _survivor_claim_owner(self) -> dict:
        """This registry's claim identity: host PID + start time + a per-registry nonce.

        The nonce separates registries sharing a PID (tests, multiplexers) and an
        exec'd predecessor, which keeps both the PID and its start time.
        """
        nonce = getattr(self, "_survivor_claim_nonce", None)
        if nonce is None:
            import uuid
            import weakref
            nonce = self._survivor_claim_nonce = uuid.uuid4().hex
            _LIVE_CLAIM_NONCES.add(nonce)
            weakref.finalize(self, _LIVE_CLAIM_NONCES.discard, nonce)
        pid = os.getpid()
        return {"pid": pid, "start": self._safe_host_start_time(pid), "nonce": nonce}

    def _survivor_claim_is_live(self, owner) -> bool:
        """Whether a recorded claim still has a live owner that will deliver it.

        Owner-lifetime lease: a slow live owner must never be overtaken by an
        expiry timeout (that would double deliver). A crashed owner's PID is
        gone or recycled, so its claim is stale.
        """
        if not isinstance(owner, dict):
            return False  # unreadable claims have no owner left to wait for
        pid = owner.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        if pid == os.getpid():
            return owner.get("nonce") in _LIVE_CLAIM_NONCES
        return self._host_pid_is_ours(pid, owner.get("start"))

    def _survivor_delivery_lock(self, session: ProcessSession):
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def held():
            path = Path(session.delivered_path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path.with_name("delivery.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                yield path
        return held()

    def _retain_survivor_completion(self, session: ProcessSession) -> bool:
        """Keep both agent and text-watcher results until their delivery is settled."""
        # Prune paths also see foreign/legacy session objects without the survival fields.
        return (is_restart_safe(session)
                and (getattr(session, "notify_on_complete", False) or getattr(session, "watcher_interval", 0) > 0)
                and self._survivor_delivery_pending(session))

    def _retain_drain_completion(self, session: ProcessSession) -> bool:
        """Carry a persistent job's collected result through the existing checkpoint handoff.

        The gateway may have cancelled its queue consumer before agents drain.
        This is a one-startup result handoff, not a restart-safe delivery lease.
        Jobs killed by the shutdown sweep must not replay as natural completions.
        """
        stopped = getattr(self, "_survivor_pollers_stopped", None)
        return (stopped is not None and stopped.is_set()
                and getattr(session, "persist_on_release", False)
                and getattr(session, "completion_reason", "") != "killed"
                and not is_restart_safe(session) and has_exit_marker(session)
                and getattr(session, "notify_on_complete", False)
                and not getattr(session, "_completion_delivered", False)
                and session.id not in self._completion_consumed)

    def _survivor_delivery_pending(self, session: ProcessSession) -> bool:
        """Read-only producer check: a queued event is not delivered and may replay after a crash."""
        return self._survivor_delivery_state(session) != "delivered"

    def _claim_survivor_delivery(self, session: ProcessSession) -> bool:
        """Atomically claim the right to render/admit this completion, before doing so.

        flock serializes the marker transition across processes: pending -> claimed(owner)
        -> delivered. Exactly one live registry holds the claim; the consumer writes
        ``delivered`` after render/admission. A claim whose owner died before
        delivery is reclaimed by a successor. Re-claiming one's own claim is
        idempotent (retries); an ambiguous post-delivery crash may replay.
        """
        if not session.delivered_path:
            return True
        try:
            from utils import atomic_json_write
            mine = self._survivor_claim_owner()
            with self._survivor_delivery_lock(session) as path:
                payload = self._read_survivor_marker(session)
                if self._marker_delivered(payload):
                    return False
                if payload is not None:
                    owner = payload.get("owner")
                    if (isinstance(owner, dict) and owner.get("nonce") == mine["nonce"]
                            and owner.get("pid") == mine["pid"]):
                        return True
                    if self._survivor_claim_is_live(owner):
                        return False
                atomic_json_write(path, {"state": "claimed", "owner": mine}, mode=0o600, fsync_dir=True)
            return True
        except OSError:
            # Fail closed: an unwritable claim must not let two gateways both deliver.
            logger.warning("Could not claim survivor completion %s", session.id, exc_info=True)
            return False

    def claim_survivor_completion(self, session_id: str) -> bool:
        """Fence a consumer before rendering/admission; ordinary completions need no lease."""
        session = self.get(session_id)
        return session is None or not is_restart_safe(session) or self._claim_survivor_delivery(session)

    def release_survivor_completion(self, session_id: str) -> bool:
        """Return failed delivery to pending, only while we still own its lease."""
        session = self.get(session_id)
        if session is None or not is_restart_safe(session) or not session.delivered_path:
            return False
        try:
            mine = self._survivor_claim_owner()
            with self._survivor_delivery_lock(session) as path:
                payload = self._read_survivor_marker(session) or {}
                owner = payload.get("owner")
                if (payload.get("state") != "claimed" or not isinstance(owner, dict)
                        or owner.get("nonce") != mine["nonce"] or owner.get("pid") != mine["pid"]):
                    return False
                path.unlink()
            return True
        except OSError:
            logger.warning("Could not release survivor completion %s", session.id, exc_info=True)
            return False

    def _mark_survivor_delivered(self, session: ProcessSession) -> bool:
        """Only the claim owner acknowledges delivery, even when another owner died."""
        if not session.delivered_path:
            return True
        try:
            from utils import atomic_json_write
            mine = self._survivor_claim_owner()
            with self._survivor_delivery_lock(session) as path:
                payload = self._read_survivor_marker(session) or {}
                owner = payload.get("owner")
                if (payload.get("state") != "claimed" or not isinstance(owner, dict)
                        or owner.get("nonce") != mine["nonce"] or owner.get("pid") != mine["pid"]):
                    return False
                atomic_json_write(path, {"state": "delivered", "delivered": True}, mode=0o600, fsync_dir=True)
            self._write_checkpoint()
            return True
        except OSError:
            logger.warning("Could not record delivered survivor completion %s", session.id, exc_info=True)
            return False

    def acknowledge_survivor_completion(self, session_id: str) -> None:
        session = self.get(session_id)
        if session is not None and is_restart_safe(session):
            self._mark_survivor_delivered(session)
        elif session is not None and session.persist_on_release:
            # Known delivery must not become a new shutdown handoff. This does
            # not consume wait/log output or create a durable delivered marker.
            with self._lock:
                session._completion_delivered = True
            stopped = getattr(self, "_survivor_pollers_stopped", None)
            if stopped is not None and stopped.is_set():
                self._write_checkpoint()

    def _consume_survivor_completion(self, session_id: str) -> None:
        """An explicit wait/log claims first rather than settling a peer's lease."""
        if self.claim_survivor_completion(session_id):
            self.acknowledge_survivor_completion(session_id)

    def _read_survivor_log(self, session: ProcessSession, *, final: bool = False) -> None:
        """One owner updates the file offset, decoder and output/watch sinks at a time."""
        with session._survivor_read_lock:
            watch_state = (session._watch_hits, session._watch_suppressed, session._watch_disabled,
                           session._watch_cooldown_until, session._watch_strike_candidate,
                           session._watch_consecutive_strikes)
            self._read_survivor_log_locked(session, final=final)
            changed = watch_state != (session._watch_hits, session._watch_suppressed, session._watch_disabled,
                                      session._watch_cooldown_until, session._watch_strike_candidate,
                                      session._watch_consecutive_strikes)
        if changed:
            self._write_checkpoint()

    def _read_survivor_log_locked(self, session: ProcessSession, *, final: bool = False) -> None:
        """Drain live unread bytes in bounded chunks; adoption seeds its own bounded tail."""
        if not session.log_path:
            return
        try:
            with Path(session.log_path).open("rb") as log_file:
                size = os.fstat(log_file.fileno()).st_size
                offset = getattr(session, "_survivor_log_offset", 0)
                if size < offset:
                    offset = 0
                    session._survivor_decoder = None
                    with session._lock:
                        session.output_buffer = ""
                if session._survivor_decoder is None:
                    session._survivor_decoder = codecs.getincrementaldecoder("utf-8")("replace")
                decoder = session._survivor_decoder
                log_file.seek(offset)
                # Snapshot EOF so a busy writer cannot starve status/stop checks.
                # Never seek past unread output: watches and live sinks need it.
                while log_file.tell() < size:
                    raw = log_file.read(min(4096, session.max_output_chars, size - log_file.tell()))
                    if not raw:
                        break
                    session._survivor_log_offset = log_file.tell()
                    self._ingest_survivor_output(session, decoder.decode(raw))
                if final:
                    self._ingest_survivor_output(session, decoder.decode(b"", final=True))
        except OSError:
            return

    def _adopt_survivor_log_tail(self, session: ProcessSession, *, final: bool = False) -> None:
        """Restore history, not live events: bounded boot IO and no replayed watch hits."""
        if not session.log_path:
            return
        with session._survivor_read_lock:
            try:
                with Path(session.log_path).open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    start = max(0, size - session.max_output_chars)
                    stream.seek(start)
                    raw = stream.read(min(size, session.max_output_chars))
                    session._survivor_log_offset = start + len(raw)
                session._survivor_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                text = session._survivor_decoder.decode(raw, final=final)
                if start == 0:
                    text = self._clean_shell_noise(text)
                session._survivor_head_noise = False
                session.append_output(text)
            except OSError:
                return

    def _ingest_survivor_output(self, session: ProcessSession, text: str) -> None:
        if session._survivor_head_noise:
            text = self._clean_shell_noise(text)
            session._survivor_head_noise = not text.strip()
        self._ingest_output(session, text)

    @staticmethod
    def _file_backed(session: ProcessSession) -> bool:
        """Collect from files only when no live pipe reader owns this session's output."""
        return has_exit_marker(session)

    def prune_survivor_run_dir(self, session: ProcessSession) -> None:
        """Prune an aged survivor only after its completion was delivered.

        A file-backed persist_on_release job has no delivery marker: its run directory
        goes when the finished session itself is pruned."""
        if is_restart_safe(session):
            if self._survivor_delivery_state(session) == "delivered":
                self._remove_survivor_run_dir(session)
        elif has_exit_marker(session):
            self._remove_survivor_run_dir(session)

    def _prune_delivered_survivor_runs(self) -> None:
        """Bound delivered artifacts even if their registry died before TTL pruning."""
        from tools import process_registry as registry
        run_root = registry.get_hermes_home() / "process-runs"
        # This sweep also runs under the registry lock during spawn/drain. Bound its
        # directory IO per profile, not per session, without delaying another profile.
        sweep_times = getattr(self, "_survivor_sweep_times", None)
        if sweep_times is None:
            sweep_times = self._survivor_sweep_times = {}
        sweep_now = time.monotonic()
        last = sweep_times.get(run_root)
        if last is not None and sweep_now - last < 60.0:
            return
        sweep_times[run_root] = sweep_now
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
                    payload = json.loads(marker.read_text(encoding="utf-8-sig"))
                    if isinstance(payload, dict) and payload.get("delivered") is True:
                        shutil.rmtree(run_dir)
                except (OSError, ValueError, TypeError):
                    continue
        except OSError:
            return

    def stop_survivor_pollers(self) -> None:
        """Leave restart-safe completions for adoption; ordinary pollers keep draining."""
        stopped = getattr(self, "_survivor_pollers_stopped", None)
        fence = getattr(self, "_survivor_poller_fence", None)
        if stopped is not None:
            if fence is None:
                stopped.set()
            else:
                with fence:
                    stopped.set()

    def _queue_survivor_completion(self, notification: dict) -> None:
        """Fence only queue publication, never a finalization owner's receipt IO."""
        stopped = getattr(self, "_survivor_pollers_stopped", None)
        fence = getattr(self, "_survivor_poller_fence", None)
        if fence is None:
            if stopped is None or not stopped.is_set():
                self.completion_queue.put(notification)
            return
        with fence:
            if stopped is None or not stopped.is_set():
                self.completion_queue.put(notification)

    def _survivor_poller_stopped(self, session: ProcessSession) -> bool:
        """Only restart-safe jobs have a successor responsible for shutdown completions."""
        stopped = getattr(self, "_survivor_pollers_stopped", None)
        return is_restart_safe(session) and stopped is not None and stopped.is_set()

    def _finish_survivor_if_allowed(self, session: ProcessSession, exit_code, reason: str, source: str) -> bool:
        """Fence restart-safe finalization only; ordinary drain results belong here."""
        if self._survivor_poller_stopped(session):
            return False
        # Shutdown can win during receipt IO. Publication rechecks under its short fence,
        # leaving the checkpoint pending for the successor instead of blocking teardown.
        self._finish_survivor(session, exit_code, reason, source)
        return True

    def _poll_persistent_detached(self, session: ProcessSession) -> bool:
        """Apply base PID/scope recovery to an unrecorded persistent job."""
        if not session.detached or is_restart_safe(session):
            return False
        fate = self._detached_host_fate(session.pid, session.host_start_time)
        if fate == "running":
            return False
        if fate == "reused":
            self._close_reused_detached(session)
            return True
        from tools.process_registry import _stop_systemd_unit
        if session.systemd_unit and not _stop_systemd_unit(session.systemd_unit):
            return True  # Keep ownership/checkpoint and retry; never lose a live scope.
        self._read_survivor_log(session, final=True)
        self._finish_recovered_exit(session, fate, self._recorded_survivor_exit(session.exit_path), {})
        return True

    def _survivor_poller_loop(self, session: ProcessSession):
        """Collect a file-backed local survivor after this or a successor gateway boots."""
        if self._survivor_poller_stopped(session):
            return
        exit_path = Path(session.exit_path)
        while not session.exited:
            if self._survivor_poller_stopped(session):
                return
            if session._kill_requested:
                time.sleep(0.05)
                continue
            try:
                exit_text = ""
                if exit_path.exists():
                    exit_text = exit_path.read_text(encoding="utf-8-sig", errors="replace").strip()
                self._read_survivor_log(session)
                if exit_text:
                    try:
                        exit_code = int(exit_text.splitlines()[-1])
                    except ValueError:
                        exit_code = -1
                    if not self._finish_survivor_if_allowed(session, exit_code, "exited", ""):
                        return
                    return
                if self._poll_persistent_detached(session):
                    if not session.exited:
                        time.sleep(0.1)
                    continue
                alive = (
                    session.process.poll() is None
                    if session.process is not None
                    else self._detached_host_fate(session.pid, session.host_start_time) == "running"
                )
                if not alive:
                    # Give the wrapper's final printf a short chance to become visible before
                    # reporting a genuinely lost process.
                    time.sleep(0.05)
                    exit_text = exit_path.read_text(encoding="utf-8-sig", errors="replace").strip() if exit_path.exists() else ""
                    if not exit_text:
                        # SIGKILL can bypass the wrapper's final write. Its original
                        # parent still owns a real wait status, so persist that rather
                        # than discarding it as a lost detached result.
                        code = session.process.poll() if session.process is not None else None
                        if code is not None:
                            from utils import atomic_write_text
                            atomic_write_text(session.exit_path, f"{code}\n", mode=0o600, fsync_dir=True)
                        if not self._finish_survivor_if_allowed(
                            session, code, "exited" if code is not None else "lost",
                            "" if code is not None else ("survivor_lost" if is_restart_safe(session) else "checkpoint.exit_uncollected"),
                        ):
                            return
                        return
            except OSError:
                self._finish_survivor_if_allowed(
                    session, None, "lost",
                    "survivor_lost" if is_restart_safe(session) else "checkpoint.exit_uncollected",
                )
                return
            time.sleep(0.1)

    @staticmethod
    def _recorded_survivor_exit(exit_path: str | None) -> int | None:
        """The wrapper's recorded exit code, or None when it never wrote one."""
        try:
            text = Path(exit_path).read_text(encoding="utf-8-sig", errors="replace").strip() if exit_path else ""
        except OSError:
            return None
        if not text:
            return None
        try:
            return int(text.splitlines()[-1])
        except ValueError:
            return -1

    @staticmethod
    def _survivor_termination_path(exit_path: str | None) -> Path | None:
        return Path(exit_path).with_name("termination") if exit_path else None

    @classmethod
    def _recorded_survivor_termination(cls, exit_path: str | None) -> dict:
        path = cls._survivor_termination_path(exit_path)
        if path is None:
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _write_survivor_termination(cls, session: ProcessSession, reason: str, source: str) -> None:
        path = cls._survivor_termination_path(session.exit_path)
        if path is None:
            return
        try:
            from utils import atomic_json_write, atomic_write_text
            payload = {
                "completion_reason": reason,
                "termination_source": source,
                "exit_code": session.exit_code,
            }
            atomic_json_write(path, payload, mode=0o600, fsync_dir=True)
            # A killed wrapper may be SIGKILLed before it can write its own exit marker.
            # Persist the operator's result as the durable fallback for recovery too.
            if has_exit_marker(session):
                atomic_write_text(
                    session.exit_path, f"{session.exit_code}\n", mode=0o600, fsync_dir=True,
                )
        except OSError:
            logger.warning("Could not persist survivor termination for %s", session.id, exc_info=True)

    def _collect_recorded_exit(self, session: ProcessSession, *, deadline=None) -> bool:
        """A published wrapper result wins over a later kill of its already-reaped PID."""
        if session.exited:
            return True
        if not has_exit_marker(session):
            return False
        exit_code = self._recorded_survivor_exit(session.exit_path)
        if exit_code is None:
            return False
        termination = self._recorded_survivor_termination(session.exit_path) if is_restart_safe(session) else {}
        previous_deadline = session._kill_deadline
        if deadline is not None:
            session._kill_deadline = deadline
        try:
            self._finish_survivor(
                session, exit_code, termination.get("completion_reason", "exited"),
                termination.get("termination_source", ""),
            )
        finally:
            session._kill_deadline = previous_deadline
        return True

    def _finish_survivor(self, session: ProcessSession, exit_code, reason: str, source: str) -> None:
        self._read_survivor_log(session, final=True)
        self._finish_exited(session, exit_code, reason=reason, source=source)
