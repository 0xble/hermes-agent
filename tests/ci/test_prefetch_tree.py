"""Base measurement must not trigger one network fetch per promised blob."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import hermes_yaml
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def partial_base(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.name", "Base Measurement Test")
    _git(origin, "config", "user.email", "ci@example.invalid")
    _git(origin, "config", "uploadpack.allowFilter", "true")
    (origin / "retired.bin").write_bytes(b"historical artifact" * 1000)
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "Historical artifact")
    retired = _git(origin, "rev-parse", "HEAD:retired.bin")
    (origin / "retired.bin").unlink()
    for i in range(32):
        (origin / f"module_{i}.py").write_text(f"value = 'HERMES_BASE_{i}'\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "Base")
    base = _git(origin, "rev-parse", "HEAD")
    _git(origin, "checkout", "-b", "topic")
    for i in range(32):
        (origin / f"module_{i}.py").write_text(f"value = 'HERMES_HEAD_{i}'\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "Wide change")
    # The trusted base gains CI tooling after this PR branched; HEAD lacks it.
    _git(origin, "checkout", "main")
    helper = origin / "scripts/ci/prefetch_tree.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/ci/prefetch_tree.py", helper)
    _git(origin, "add", "scripts/ci/prefetch_tree.py")
    _git(origin, "commit", "-m", "Add trusted CI helper")
    _git(origin, "checkout", "topic")
    candidate = tmp_path / "candidate"
    _git(tmp_path, "clone", "--no-checkout", "--filter=blob:none", origin.as_uri(), str(candidate))
    _git(candidate, "checkout", "--detach", "HEAD")
    # Isolate the batch hydration count from fetching the base-owned helper itself.
    _git(candidate, "show", "origin/main:scripts/ci/prefetch_tree.py")
    missing = _git(candidate, "rev-list", "--objects", "--missing=print", base + "^{tree}")
    assert sum(line.startswith("?") for line in missing.splitlines()) == 32
    return candidate, base, retired


def _prepare(repo, **kwargs):
    workflow = hermes_yaml.safe_load((ROOT / ".github/workflows/gate.yml").read_text())
    ratchet = next(step for step in workflow["jobs"]["linux"]["steps"]
                   if step.get("name") == "Code health ratchet (blocking)")
    # Execute the real workflow preparation, stopping before tool installation/measurement.
    prepare = ratchet["run"].split("uv run", 1)[0]
    runner_temp = repo.parent / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    env = {**os.environ, **kwargs.pop("env", {}), "RUNNER_TEMP": str(runner_temp)}
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", prepare], cwd=repo, env=env, **kwargs,
    )


def test_prefetch_batches_only_base_tree_and_allows_offline_measurement(partial_base, tmp_path):
    repo, base, retired = partial_base
    trace = tmp_path / "trace"
    assert not (repo / "scripts/ci/prefetch_tree.py").exists()
    _prepare(repo, check=True, env={**os.environ, "GIT_TRACE": str(trace)})
    fetches = [line for line in trace.read_text().splitlines() if "built-in: git fetch" in line]
    assert len(fetches) == 1, "base blobs must be fetched together, not per object"
    assert "?" + retired in _git(repo, "rev-list", "--objects", "--all", "--missing=print").splitlines()
    _git(repo, "remote", "set-url", "origin", (tmp_path / "offline").as_uri())
    names = _git(repo, "grep", "-h", "-o", "-E", "HERMES_[A-Z0-9_]+", base, "--", "*.py")
    assert {line.rsplit(":", 1)[-1] for line in names.splitlines()} == {f"HERMES_BASE_{i}" for i in range(32)}
    assert _git(repo, "show", base + ":module_0.py") == "value = 'HERMES_BASE_0'"
    # A warm tree is a no-op offline, and PR-owned tooling must never run.
    untrusted = repo / "scripts/ci/prefetch_tree.py"
    untrusted.parent.mkdir(parents=True)
    untrusted.write_text("raise SystemExit('PR-owned helper executed')\n")
    _prepare(repo, check=True)


def test_prefetch_fails_closed_when_promised_base_is_unavailable(partial_base, tmp_path):
    repo, _, _ = partial_base
    _git(repo, "remote", "set-url", "origin", (tmp_path / "offline").as_uri())
    result = _prepare(repo, capture_output=True, text=True)
    assert result.returncode != 0
    assert "offline" in result.stderr
