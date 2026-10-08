"""Real candidate web packaging coverage for immutable release staging."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases


@pytest.mark.platforms("macos")
def test_real_candidate_web_build_from_staged_revision(tmp_path):
    """The production candidate web build must emit the staged revision's bundle."""
    repo = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()
    staging = tmp_path / "staging"
    releases._stage_git_tree(repo, staging, sha)

    releases._build_candidate_web(staging)

    assert (staging / "hermes_cli" / "web_dist" / "index.html").is_file()
