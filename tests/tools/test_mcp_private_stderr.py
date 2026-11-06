"""Private stderr uses real owned pipes and normal saving resumes."""
import os
from pathlib import Path
PRIVATE = 'SYNTHETIC_PRIVATE_INGRESS_8337'
NORMAL = 'synthetic-normal-idle-ingress'

def test_private_stderr_pipe_is_volatile_then_normal_pipe_persists(tmp_path):
    from tools.mcp_tool_config import _StderrTee
    private = _StderrTee(None)
    private.sink.write((PRIVATE + '\n').encode())
    assert private.close() == PRIVATE + '\n'
    assert not private._reader.is_alive()
    path = tmp_path / 'normal-stderr.log'
    with path.open('w') as sink:
        normal = _StderrTee(sink)
        normal.sink.write((NORMAL + '\n').encode())
        assert normal.close() == NORMAL + '\n'
        assert not normal._reader.is_alive()
    assert NORMAL in path.read_text() and PRIVATE not in path.read_text()
    if output := os.environ.get('AUX_RUN_DIR'):
        (Path(output) / 'normal-resumed-stderr.log').write_bytes(path.read_bytes())
