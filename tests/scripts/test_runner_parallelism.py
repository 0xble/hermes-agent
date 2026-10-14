"""The canonical shell runner gives compilation and pytest the same bounded budget."""
from pathlib import Path
import json
import os
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("workers", "expected"), [(None, "4"), ("2", "2"), ("4", "4")])
def test_runner_bounds_compilation_and_forwards_default_to_clean_env(tmp_path, workers, expected):
    python = tmp_path / "python"
    log = tmp_path / "calls.jsonl"
    python.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "if sys.argv[1:2] != ['-c']:\n"
        f"    with open({str(log)!r}, 'a') as out:\n"
        "        out.write(json.dumps({'args': sys.argv[1:], 'workers': os.environ.get('HERMES_TEST_WORKERS')}) + '\\n')\n"
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_PYTHON": str(python)}
    if workers is not None:
        env["HERMES_TEST_WORKERS"] = workers
    result = subprocess.run(["bash", str(ROOT / "scripts/run_tests.sh"), "tests/example.py"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    compilation, runner = [json.loads(line) for line in log.read_text().splitlines()]
    assert compilation["args"][:5] == ["-m", "compileall", "-q", "-j", expected]
    assert runner["workers"] == expected
    assert runner["args"][1:] == ["tests/example.py"]
