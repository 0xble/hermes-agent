"""Real candidate web packaging coverage for immutable release staging."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases


def _git(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()


@pytest.mark.platforms("macos")
def test_real_candidate_web_build_from_staged_revision(tmp_path):
    """The production candidate web build must emit the staged revision's bundle.

    The staged commit carries a marker the source working tree lacks, and the
    working tree carries a different marker the commit lacks. Only a build of
    the archived revision contains exactly the staged marker.
    """
    repo = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(repo), str(source)], check=True)
    index = source / "web" / "index.html"
    original = index.read_text(encoding="utf-8")
    staged_marker = f"staged-{uuid.uuid4().hex}"
    source_marker = f"source-{uuid.uuid4().hex}"

    def with_marker(marker: str) -> str:
        tag = f'<meta name="hermes-revision-marker" content="{marker}" />'
        assert "</head>" in original
        return original.replace("</head>", f"  {tag}\n  </head>", 1)

    index.write_text(with_marker(staged_marker), encoding="utf-8")
    _git(source, "add", "web/index.html")
    _git(source, "-c", "user.name=Web", "-c", "user.email=web@example.test",
         "-c", "core.hooksPath=/dev/null", "commit", "-qm", "test: staged web marker")
    sha = _git(source, "rev-parse", "HEAD")
    # Diverge the checkout from the revision being staged.
    index.write_text(with_marker(source_marker), encoding="utf-8")

    staging = tmp_path / "staging"
    releases._stage_git_tree(source, staging, sha)

    releases._build_candidate_web(staging)

    built = (staging / "hermes_cli" / "web_dist" / "index.html").read_text(encoding="utf-8")
    assert staged_marker in built, "candidate web bundle was not built from the staged revision"
    assert source_marker not in built, "candidate web bundle was built from the source checkout"
    assert not (source / "hermes_cli" / "web_dist").exists(), "candidate build wrote into the source checkout"
