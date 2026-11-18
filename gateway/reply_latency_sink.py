"""Process-owned JSONL evidence for the local factory KPI reader."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import psutil

from hermes_constants import get_routing_process_hermes_home


def log_directory() -> Path:
    # Profile labels share one process sequence. A routed ContextVar must not
    # scatter that sequence across homes, or the consumer sees false gaps.
    return get_routing_process_hermes_home() / "logs" / "reply-latency"


@lru_cache(maxsize=128)
def _ensure_log_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)


def _append(directory: Path, row: dict) -> None:
    _ensure_log_directory(directory)
    day = datetime.fromtimestamp(row["observed_at"], UTC).date().isoformat()
    path = directory / f"{day}.jsonl"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except FileNotFoundError:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        payload = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        if os.write(fd, payload) != len(payload):
            raise OSError("Incomplete reply latency append")
    finally:
        os.close(fd)


@dataclass
class _ProcessSink:
    process_id: str
    started_at: float
    sequence: int = 0
    coverage_start: float | None = None

    def write(self, directory: Path, row: dict, observed_at: float) -> None:
        # Reserve before I/O: a failed append leaves a detectable sequence gap.
        self.sequence += 1
        _append(directory, {**row, "schema": 1, "process_id": self.process_id,
                            "started_at": self.started_at, "sequence": self.sequence,
                            "observed_at": observed_at})


_lock = threading.Lock()
_sinks: dict[tuple[int, Path], _ProcessSink] = {}
_instance = uuid.uuid4().hex


def emit(directory: Path, row: dict, observed_at: float) -> None:
    with _lock:
        key = (os.getpid(), directory)
        if key not in _sinks:
            _sinks[key] = _ProcessSink(f"{key[0]}:{_instance}", psutil.Process().create_time())
        sink = _sinks[key]
        profile = row["profile"]
        if sink.coverage_start is None:
            sink.write(directory, {"event": "instrumentation_start", "profile": profile,
                                   "timestamp": observed_at}, observed_at)
            sink.coverage_start = observed_at
        if row["event"] != "coverage":
            sink.write(directory, row, observed_at)
        sink.write(directory, {"event": "coverage", "profile": profile,
                               "timestamp": observed_at, "coverage_start": sink.coverage_start}, observed_at)
        sink.coverage_start = observed_at
