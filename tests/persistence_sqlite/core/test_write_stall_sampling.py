"""Stall capture observes shared phase stamps, never a database or writer lock."""

import importlib
import subprocess
from pathlib import Path

import pytest

from tests.hermes_state.test_transcript_write_diagnostics import _Clock


def _watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    stalls = importlib.import_module("hermes_state_write_stalls")
    clock = _Clock()
    clock.wall = 100.0
    monkeypatch.setattr(stalls, "time", clock)
    return stalls, clock, stalls.WriteStallWatchdog(threshold_s=10.0)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("phase", ["callback", "commit"])
def test_capture_stalled_phase_once_and_rate_limit_across_writes(tmp_path, monkeypatch, phase):
    stalls, clock, watcher = _watcher(tmp_path, monkeypatch)
    commands = []

    def sample(argv, **kwargs):
        commands.append(argv)
        Path(argv[-1]).write_text("native stack", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(stalls.subprocess, "run", sample)
    try:
        slot = watcher.begin(phase, "transcript.append")
        watcher.poll(109.0)
        assert commands == []
        watcher.poll(111.0)
        assert len(commands) == 1
        assert commands[0][:4] == ["/usr/bin/sample", str(stalls.os.getpid()), "1", "-file"]
        # Even a continuing 11-minute stall is sampled only once.
        watcher.poll(711.0)
        assert len(commands) == 1
        watcher.end(slot)
        clock.wall = 200.0
        slot = watcher.begin(phase, "transcript.append")
        watcher.poll(211.0)
        assert len(commands) == 1  # a different write cannot bypass ten-minute throttling
        watcher.end(slot)
        clock.wall = 712.0
        slot = watcher.begin(phase, "transcript.append")
        watcher.poll(723.0)
        assert len(commands) == 2
        watcher.end(slot)
        watcher.poll(2000.0)
        assert len(commands) == 2
        paths = list((tmp_path / "logs" / "write-stalls").glob("write-stall-*.txt"))
        assert len(paths) == 2
        assert all(path.read_text(encoding="utf-8") == "native stack" for path in paths)
    finally:
        watcher.close()


@pytest.mark.platforms("macos")
def test_no_in_flight_phase_never_runs_sampler(tmp_path, monkeypatch):
    stalls, clock, watcher = _watcher(tmp_path, monkeypatch)
    commands = []
    monkeypatch.setattr(stalls.subprocess, "run", lambda *args, **kwargs: commands.append(args))
    try:
        watcher.poll(1000.0)
        slot = watcher.begin("commit", "transcript.append")
        clock.wall = 105.0
        watcher.end(slot)
        watcher.poll(2000.0)
        assert commands == []
        assert not (tmp_path / "logs" / "write-stalls").exists()
    finally:
        watcher.close()


@pytest.mark.platforms("macos")
def test_sampler_failure_is_warning_and_is_rate_limited(tmp_path, monkeypatch, caplog):
    stalls, clock, watcher = _watcher(tmp_path, monkeypatch)
    calls = []

    def fail(argv, **kwargs):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv, stderr="private sampler details")

    monkeypatch.setattr(stalls.subprocess, "run", fail)
    try:
        slot = watcher.begin("commit", "transcript.append")
        with caplog.at_level("WARNING", logger="hermes_state"):
            watcher.poll(111.0)
            watcher.poll(112.0)
            watcher.end(slot)
            clock.wall = 120.0
            watcher.begin("commit", "transcript.append")
            watcher.poll(131.0)
        assert len(calls) == 1
        assert any(record.levelname == "WARNING" and "native stack capture failed" in record.getMessage()
                   for record in caplog.records)
        assert "private sampler details" not in caplog.text
        assert list((tmp_path / "logs" / "write-stalls").glob("write-stall-*.txt")) == []
    finally:
        watcher.close()


@pytest.mark.platforms("macos")
def test_retention_keeps_last_twenty_owned_samples(tmp_path, monkeypatch):
    stalls, _clock, watcher = _watcher(tmp_path, monkeypatch)
    directory = tmp_path / "logs" / "write-stalls"
    directory.mkdir(parents=True)
    for index in range(20):
        path = directory / f"write-stall-old-{index:02}.txt"
        path.write_text("old", encoding="utf-8")
        stalls.os.utime(path, (index, index))
    unrelated = directory / "keep.txt"
    unrelated.write_text("operator note", encoding="utf-8")

    def sample(argv, **kwargs):
        Path(argv[-1]).write_text("new sample", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(stalls.subprocess, "run", sample)
    try:
        watcher.begin("commit", "transcript.append")
        watcher.poll(111.0)
        samples = list(directory.glob("write-stall-*.txt"))
        assert len(samples) == 20
        assert not (directory / "write-stall-old-00.txt").exists()
        assert any(path.read_text(encoding="utf-8") == "new sample" for path in samples)
        assert unrelated.read_text(encoding="utf-8") == "operator note"
    finally:
        watcher.close()
