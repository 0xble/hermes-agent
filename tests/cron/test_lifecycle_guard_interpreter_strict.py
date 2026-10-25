"""No shebang earns interpreter leniency; these old bypasses stay on the base strict walk."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("shebang", [
    "#!/usr/bin/env python3\n", "#!/usr/bin/python3\n", "#!/usr/bin/env python3\r\n",
    "#!/usr/bin/env python3 \n", "#!python3\n", "#!/usr/bin/env\n", "#!\n", "#!/bin/sh\n", "",
])
def test_direct_script_references_are_strict(tmp_path, shebang):
    blocked = tmp_path / "blocked-fifo"
    os.mkfifo(blocked)
    tool = tmp_path / "tool"
    tool.write_text(f"{shebang}{blocked}\n", encoding="utf-8")
    tool.chmod(0o755)
    unsafe, refusal = scan_gateway_lifecycle(str(tool))
    assert unsafe is True
    assert refusal is not None and str(blocked) in refusal


@pytest.mark.parametrize("shell_name", ["tcsh", "csh"])
def test_interpreter_named_python3_by_shell_symlink_is_strict(tmp_path_factory, shell_name):
    root = tmp_path_factory.mktemp("lg")
    shell = Path("/bin") / shell_name
    interpreter = root / "python3"
    interpreter.symlink_to(shell)
    child = root / "child.sh"
    child.write_text("#!/bin/sh\n" + "# pad\n" * 200_000 + "printf 'child-ran\\n'\n")
    child.chmod(0o755)
    tool = root / "tool"
    tool.write_text(f"#!{interpreter}\n{shlex.quote(str(child))}\n", encoding="utf-8")
    tool.chmod(0o755)
    if shell.is_file():
        # Do not source the real user's csh startup files while proving harmless execution.
        result = subprocess.run(
            [str(tool)], capture_output=True, text=True, timeout=10,
            env={**os.environ, "HOME": str(root)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "child-ran\n"
    unsafe, refusal = scan_gateway_lifecycle(str(tool))
    assert unsafe is True
    assert refusal is not None and str(child) in refusal


@pytest.mark.parametrize("prefix", ["PATH=/tmp/evil ", "export PATH=/tmp/evil; ", "env PATH=/tmp/evil "])
def test_env_path_override_cannot_make_a_direct_script_lenient(tmp_path, prefix):
    blocked = tmp_path / "blocked-fifo"
    os.mkfifo(blocked)
    tool = tmp_path / "tool"
    tool.write_text(f"#!/usr/bin/env python3\n{blocked}\n", encoding="utf-8")
    tool.chmod(0o755)
    unsafe, refusal = scan_gateway_lifecycle(f"{prefix}{tool}")
    assert unsafe is True
    assert refusal is not None and str(blocked) in refusal


@pytest.mark.parametrize("size", [127, 128, 600])
def test_overlong_or_boundary_shebang_stays_strict(tmp_path, size):
    blocked = tmp_path / "blocked-fifo"
    os.mkfifo(blocked)
    tool = tmp_path / "tool"
    tool.write_bytes(b"#!/" + b"a" * size + b"/python3\n" + str(blocked).encode() + b"\n")
    tool.chmod(0o755)
    assert scan_gateway_lifecycle(str(tool))[0] is True


@pytest.mark.parametrize("case", ["nul", "nested"])
def test_nul_and_nested_shebangs_stay_strict(tmp_path, case):
    blocked = tmp_path / "blocked-fifo"
    os.mkfifo(blocked)
    interpreter = tmp_path / "python3"
    interpreter.write_text(f"#!/bin/sh\n{blocked}\n", encoding="utf-8")
    interpreter.chmod(0o755)
    shebang = {
        "nul": b"#!/usr/bin/env python3\x00\n",
        "nested": f"#!{interpreter}\n".encode(),
    }[case]
    tool = tmp_path / "tool"
    tool.write_bytes(shebang + str(blocked).encode() + b"\n")
    tool.chmod(0o755)
    assert scan_gateway_lifecycle(str(tool))[0] is True
