"""Generated helpers run on older peer interpreters (regression for #913)."""
import json
from pathlib import Path
import subprocess

import pytest

from tests.tools.test_code_execution_file_rpc import CALLS
from tools.code_execution_tool import generate_hermes_tools_module


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("transport", ["uds", "file"])
def test_complete_generated_helpers_import_and_forward_on_python39(transport, tmp_path):
    python = Path("/usr/bin/python3")
    if not python.is_file():
        pytest.skip("Apple Python 3.9 is not installed")
    version = subprocess.run([str(python), "-c", "import sys; print(sys.version_info[:2])"],
                             capture_output=True, text=True, check=True)
    if version.stdout.strip() != "(3, 9)":
        pytest.skip("Apple Python is not 3.9")
    module = tmp_path / "hermes_tools.py"
    module.write_text(generate_hermes_tools_module(list(CALLS), transport=transport))
    inputs = {name: args for name, args in CALLS.items() if "node" in args}
    program = '''import hermes_tools as tools, json
calls = []
tools._call = lambda name, args: calls.append([name, args]) or args
inputs = json.loads(INPUTS)
for name, args in inputs.items():
    assert getattr(tools, name)(**args) == args
    legacy = {key: value for key, value in args.items() if key != "node"}
    assert getattr(tools, name)(**legacy) == legacy
print(json.dumps(calls))
'''.replace("INPUTS", repr(json.dumps(inputs)))
    result = subprocess.run([str(python), "-c", program], cwd=tmp_path,
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 2 * len(inputs)
