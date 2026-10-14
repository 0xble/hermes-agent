"""PM applies a bounded build/install/bytecode budget to its actual uv child."""
from pathlib import Path
import json
import os
import stat
import sys

import pytest

from pm.environment import PythonEnvironment


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("workers", "expected"), [
    (None, "4"), ("", "4"), ("banana", "4"), ("0", "4"), ("1.5", "4"),
    ("2", "2"), ("4", "4"),
])
def test_uv_child_uses_per_job_budget_without_changing_parent(tmp_path, workers, expected):
    uv = tmp_path / "uv"
    log = tmp_path / "child.json"
    uv.write_text(
        f"#!{sys.executable}\nimport os, json, pathlib\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps(dict(os.environ)))\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, UV_CONCURRENT_BUILDS="64", UV_CONCURRENT_INSTALLS="64")
    env.pop("HERMES_TEST_WORKERS", None)
    if workers is not None:
        env["HERMES_TEST_WORKERS"] = workers
    before = env.copy()
    environment = PythonEnvironment(uv=uv, python=Path(sys.executable), destination=tmp_path / "venv",
                                    cache=tmp_path / "cache", env=env)
    environment.sync(tmp_path)
    child = json.loads(log.read_text())
    assert child["UV_CONCURRENT_BUILDS"] == expected
    assert child["UV_CONCURRENT_INSTALLS"] == expected
    assert env == before
