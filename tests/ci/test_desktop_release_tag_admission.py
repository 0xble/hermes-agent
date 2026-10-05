"""Tag admission for .github/workflows/desktop-bundled-release.yml (plan item 7).

The release build runs under the ``release-signing`` environment, so the
validate job must be more than a shape check: a correctly-shaped tag on an
unreviewed commit must never reach the signing build. Two layers are tested:

* Structure — the workflow declares the admitted SHA as a job output and
  every privileged job checks out THAT, not the (moveable) tag ref.
* Behavior — the production admission command runs inside real temp git
  repositories: an annotated claim on origin/main passes and exports the full
  SHA; a claim for a commit NOT on main is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci.desktop_release_roles import (
    DOWNLOADABLE_DISPATCHES, DRY_DISPATCH, SHA, admitted, credential_gate, evaluate, gate,
    native_builds, needs_of,
)

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "desktop-bundled-release.yml"
_SIGNING_ENV = "release-signing"
_CONTROLLER = "c" * 40




# ---------------------------------------------------------------------------
# Structure: the admitted SHA is the only build input privileged jobs see.
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Behavior: run the admission script against real repositories.
# ---------------------------------------------------------------------------

bash = shutil.which("bash")
pytestmark = pytest.mark.skipif(bash is None, reason="bash is required to run the admission script")


def _native_tool(name: str) -> str:
    """Resolve *name* to an executable CreateProcess can actually start.

    run_tests.sh / conftest blank SystemRoot/ComSpec for hermeticity, and on
    this host PATH can point ``git``/``bash`` at the MSIX payload copy under
    ``C:\\Program Files\\WindowsApps\\...`` — a store-app location that
    fails CreateProcess with WinError 5 outside its package context. Prefer
    a conventional install; give children a complete environment too.
    """
    candidates = [hit for hit in (shutil.which(name),) if hit]
    if sys.platform == "win32":
        git_base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
        for rel in (("cmd", f"{name}.exe"), ("bin", f"{name}.exe"), ("usr", "bin", f"{name}.exe")):
            p = git_base.joinpath(*rel)
            if p.exists():
                candidates.append(str(p))
    for cand in candidates:
        if "windowsapps" not in cand.lower():
            return cand
    return candidates[0]


def _child_env(**overrides: str) -> dict:
    env = os.environ.copy()
    if sys.platform == "win32":
        env.setdefault("SystemRoot", r"C:\Windows")
        env.setdefault("ComSpec", r"C:\Windows\system32\cmd.exe")
        env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    env.update(overrides)
    return env


_GIT = _native_tool("git")
_BASH = _native_tool("bash")


def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        [_GIT, *args], cwd=cwd, capture_output=True, text=True, check=True,
        env=_child_env(),
    )
    return out.stdout.strip()


def _seed_repo(root: Path) -> tuple[Path, Path]:
    """origin (upstream) + clone (where releases are tagged from).

    origin/main holds a pyproject whose version matches the stable tag, so
    the pyproject-lockstep leg of the shape check passes for v0.1.2.
    """
    origin = root / "origin"
    origin.mkdir()
    _git("init", "-b", "main", cwd=origin)
    _git("config", "user.email", "ci@example.com", cwd=origin)
    _git("config", "user.name", "ci", cwd=origin)
    (origin / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.2"\n', encoding="utf-8")
    (origin / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=origin)
    _git("commit", "-m", "seed", cwd=origin)

    clone = root / "clone"
    _git("clone", str(origin), str(clone), cwd=root)
    _git("config", "user.email", "ci@example.com", cwd=clone)
    _git("config", "user.name", "ci", cwd=clone)
    return origin, clone


def _run_admission(clone: Path, tag: str, claim_tag: str) -> subprocess.CompletedProcess:
    gh_output = clone / "github_output.txt"
    gh_output.write_text("", encoding="utf-8")
    env = _child_env(
        TAG=tag,
        RELEASE_TAG=tag,
        RELEASE_CLAIM_TAG=claim_tag,
        RELEASE_CLAIM_OBJECT=_git("rev-parse", f"refs/tags/{claim_tag}", cwd=clone),
        GITHUB_OUTPUT=str(gh_output),
        RELEASE_PHASE="candidate",
        GITHUB_REF=f"refs/tags/{claim_tag}",
        GITHUB_SHA=_git("rev-parse", "HEAD", cwd=clone),
        PYTHONPATH=str(_REPO),
    )
    return subprocess.run(
        [sys.executable, "-m", "scripts.releases.stable", "verify"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _claim_message(clone: Path) -> str:
    return json.dumps({
        "schema": 1,
        "version": "0.1.2",
        "attempt": 1,
        "commit": _git("rev-parse", "HEAD", cwd=clone),
        "autopublish": False,
        "skipBundles": False,
        "skipTests": False,
        "claimEpoch": 1_790_000_000,
    }, sort_keys=True, separators=(",", ":"))


def test_claim_on_origin_main_is_admitted_and_exports_the_full_sha(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _origin, clone = _seed_repo(tmp_path)
    monkeypatch.setenv("GIT_COMMITTER_DATE", "@1790000000 +0000")
    _git("tag", "-a", "rc.1-v0.1.2", "-m", _claim_message(clone), cwd=clone)
    _git("push", "origin", "refs/tags/rc.1-v0.1.2", cwd=clone)

    proc = _run_admission(clone, "v0.1.2", "rc.1-v0.1.2")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    gh_output = (clone / "github_output.txt").read_text(encoding="utf-8")
    expected = _git("rev-parse", "HEAD", cwd=clone)
    assert f"sha={expected}" in gh_output


def test_claim_not_on_origin_main_is_refused(tmp_path: Path):
    _origin, clone = _seed_repo(tmp_path)
    # A commit that exists ONLY in the clone — never pushed, never reviewed.
    (clone / "rogue.txt").write_text("unreviewed\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "rogue", cwd=clone)
    _git("tag", "-a", "rc.1-v0.1.2", "-m", _claim_message(clone), cwd=clone)
    _git("push", "origin", "refs/tags/rc.1-v0.1.2", cwd=clone)

    proc = _run_admission(clone, "v0.1.2", "rc.1-v0.1.2")
    assert proc.returncode != 0, "a tag off origin/main must not be admitted"
    assert "is not on main" in proc.stdout + proc.stderr
    # And nothing was exported for the signing jobs to consume.
    assert "sha=" not in (clone / "github_output.txt").read_text(encoding="utf-8")


def test_malformed_claim_is_refused(tmp_path: Path):
    _origin, clone = _seed_repo(tmp_path)
    _git("tag", "-a", "v0.1.2-rc1", "-m", "bad claim", cwd=clone)
    _git("push", "origin", "refs/tags/v0.1.2-rc1", cwd=clone)
    proc = _run_admission(clone, "v0.1.2", "v0.1.2-rc1")
    assert proc.returncode != 0
    assert "is not a claim tag" in proc.stdout + proc.stderr




