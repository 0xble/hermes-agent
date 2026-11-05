"""Private append-only receipt evidence; never part of the delivery transaction."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
RETENTION_DAYS = 14
CHECKPOINT_SECONDS = 30
_sinks: dict[Path, ReplyLatencySink] = {}
_sinks_lock = threading.Lock()


class ReplyLatencySink:
    def __init__(self, home: Path, profile: str = "default"):
        self.root = Path(home) / "logs" / "reply-latency"
        self.profile = profile
        self.process_id = uuid.uuid4().hex
        self.started_at = time.time()
        self.sequence = 0
        self.day = None
        self.coverage_start: float | None = self.started_at
        self.last_warning = None
        self.lock = threading.Lock()
        self.pending: queue.Queue = queue.Queue(maxsize=1024)
        self.interrupted = threading.Event()
        self.unproven_until = 0.0
        self.worker = threading.Thread(target=self._drain, name="reply-latency-sink", daemon=True)
        self.worker.start()

    def _drain(self) -> None:
        while True:
            payload, observed_at = self.pending.get()
            try:
                if self.interrupted.is_set():
                    self.interrupted.clear()
                    self.coverage_start = None
                    self.unproven_until = time.time()
                    self._warn("reply-latency sink queue full; coverage interrupted")
                self._write(payload, observed_at)
            finally:
                self.pending.task_done()

    def write(self, payload: dict | None = None) -> None:
        # Intake and confirmed delivery never wait for the disk or its writer lock.
        try:
            self.pending.put_nowait((payload, time.time()))
        except queue.Full:
            self.interrupted.set()
            # The writer emits the rate-limited diagnostic off the delivery loop.

    def flush(self) -> None:
        """Test/process-boundary barrier; never called by delivery."""
        self.pending.join()

    def _append(self, payload: dict, now: float) -> None:
        self.sequence += 1
        row = {**payload, "schema": 1, "process_id": self.process_id,
               "started_at": self.started_at, "sequence": self.sequence,
               "observed_at": now}
        path = self.root / f"{datetime.fromtimestamp(now, timezone.utc).date()}.jsonl"
        data = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            # Enforce private permissions even if a pre-existing file was created broadly.
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            if os.write(fd, data) != len(data):
                raise OSError("short reply-latency append")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _rotate(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).date()
        if day == self.day:
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.root.chmod(0o700)
        cutoff = day - timedelta(days=RETENTION_DAYS - 1)
        for path in self.root.glob("*.jsonl"):
            try:
                date = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if path.stem == date.isoformat() and date < cutoff:
                path.unlink()
        if self.sequence == 0 and self.coverage_start is not None:
            self.coverage_start = now  # Never heal an overflow/failure invalidated before initialization.
        self._append({"event": "instrumentation_start", "timestamp": now,
                      "profile": self.profile}, now)
        self.day = day

    def _warn(self, message: str) -> None:
        now = time.time()
        if self.last_warning is None or now - self.last_warning >= 300:
            self.last_warning = now
            try:
                logger.warning(message)
            except Exception:
                pass

    def _write(self, payload: dict | None, now: float) -> None:
        with self.lock:
            # Backlogged checkpoints captured before a dropped event cannot heal its gap.
            if payload is None and now < self.unproven_until:
                return
            try:
                self._rotate(now)
                row = payload if payload is not None else {
                    "event": "coverage", "timestamp": now, "profile": self.profile,
                    "coverage_start": self.coverage_start if self.coverage_start is not None else now}
                self._append(row, now)
                if payload is None:
                    self.coverage_start = now
            except Exception:
                # A subsequent successful checkpoint must not attest the failed interval.
                self.coverage_start = None
                self._warn("reply-latency sink unavailable; coverage interrupted")

    def checkpoint(self) -> None:
        self.write()


def sink_for_home(home: Path, profile: str = "default") -> ReplyLatencySink:
    # Homes arrive from resolved runtime identity. Lexical normalization only:
    # Path.resolve/stat would put filesystem latency back on the delivery loop.
    home = Path(os.path.abspath(home))
    with _sinks_lock:
        if home not in _sinks:
            _sinks[home] = ReplyLatencySink(home, profile)
        return _sinks[home]


def append_event(home: Path, payload: dict) -> None:
    sink_for_home(home, payload["profile"]).write(payload)


def checkpoint_profiles(homes: dict[str, Path]) -> None:
    for profile, home in homes.items():
        sink_for_home(home, profile).checkpoint()


async def coverage_forever(runner) -> None:
    # Capture the launch home before the thread hop; served homes are explicit.
    from hermes_constants import get_hermes_home
    launch_home = get_hermes_home()
    launch_profile = getattr(runner, "_primary_profile_name", None) or "default"
    while runner._running:
        homes = dict(getattr(runner, "_served_profile_homes", None) or {launch_profile: launch_home})
        await asyncio.to_thread(checkpoint_profiles, homes)
        await asyncio.sleep(CHECKPOINT_SECONDS)
