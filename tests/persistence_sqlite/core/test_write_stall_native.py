"""The external helper can capture even while a C call owns the writer's GIL."""

import os
import subprocess
import sys

import pytest

from tests.waiting import wait_until


@pytest.mark.platforms("macos")
def test_native_capture_while_target_cannot_run_python(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    read_fd, write_fd = os.pipe()
    code = """
import ctypes
import sys
from pathlib import Path
from hermes_state_write_stalls import observe_write
with observe_write(Path(sys.argv[2]) / 'unused.db', lambda conn: None):
    from hermes_state_write_stalls import current_observation
    current_observation.get().phase('commit')
    # PyDLL deliberately retains the GIL for the entire blocking native read.
    libc = ctypes.PyDLL(None)
    buffer = ctypes.create_string_buffer(1)
    libc.read(int(sys.argv[1]), buffer, 1)
    current_observation.get().end()
"""
    directory = tmp_path / "logs" / "write-stalls"
    child = subprocess.Popen([sys.executable, "-c", code, str(read_fd), str(tmp_path)], pass_fds=(read_fd,),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(read_fd)
    try:
        def captured():
            return any(path.stat().st_size > 0 for path in directory.glob("write-stall-*.txt"))

        assert wait_until(captured, timeout=25), "external sampler did not capture the GIL-blocked target"
    finally:
        os.write(write_fd, b"x")
        os.close(write_fd)
        output, errors = child.communicate(timeout=10)
    assert child.returncode == 0, (output, errors)
    sample, = directory.glob("write-stall-*.txt")
    assert f"write-stall-{child.pid}-" in sample.name
    assert "read" in sample.read_text(encoding="utf-8")
    assert sample.stat().st_mode & 0o777 == 0o600
