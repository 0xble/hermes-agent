"""Fork upgrade fixtures follow promoted releases, not unrelated upstream CalVer tags.

#123254/#126150: the upstream September updater predates shallow healing, even though the
fork has already promoted that fix. Exercise release selection with real repositories.
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.platforms("posix")


def _git(root, *args):
    cp = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return cp.stdout.strip()


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    from tests.e2e.core.upgrade.git import _git_world as G

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    commits = []
    for i in range(5):
        _git(source, "commit", "-q", "--allow-empty", "-m", f"release {i}")
        commits.append(_git(source, "rev-parse", "HEAD"))
    _git(source, "tag", "v2026.9.24", commits[0])
    remote = tmp_path / "origin.git"
    _git(source, "clone", "-q", "--bare", str(source), str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    monkeypatch.setattr(G.H, "WORKTREE", source)
    monkeypatch.setattr(G, "FORK_RELEASE_FLOOR", commits[1], raising=False)
    clear = getattr(G.n1_tag, "cache_clear", lambda: None)
    clear()
    yield G, source, remote, commits
    clear()


def test_n1_follows_the_newest_promoted_strict_ancestor_not_upstream_tags(release_repo):
    G, source, remote, commits = release_repo
    for date, sha in (("20261101", commits[1]), ("20261102", commits[3]),
                      ("20261103", commits[4])):
        _git(remote, "update-ref", f"refs/heads/deploy/nightly-{date}", sha)
    # A newer promotion outside this candidate's ancestry cannot be its N-1.
    _git(source, "checkout", "-q", "-b", "unrelated", commits[0])
    _git(source, "commit", "-q", "--allow-empty", "-m", "other lineage")
    _git(source, "push", "-q", "origin", "HEAD:refs/heads/deploy/nightly-20261104")
    _git(source, "checkout", "-q", "main")
    assert G.n1_tag() == commits[3]


def test_unfetchable_newer_promotion_does_not_hide_an_available_ancestor(release_repo, monkeypatch):
    G, source, remote, commits = release_repo
    _git(remote, "update-ref", "refs/heads/deploy/nightly-20261102", commits[3])
    real_run = subprocess.run
    missing = "e" * 40

    def unavailable(argv, **kwargs):
        if argv[1:2] == ["fetch"] and argv[-1] == missing:
            raise subprocess.TimeoutExpired(argv, 30)
        cp = real_run(argv, **kwargs)
        if argv[1:2] == ["ls-remote"]:
            cp.stdout += f"{missing}\trefs/heads/deploy/nightly-20261103\n"
        return cp

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert G.n1_tag() == commits[3]


def test_unavailable_promotions_use_the_floor_or_fail_loudly(release_repo, monkeypatch):
    G, source, remote, commits = release_repo
    _git(source, "remote", "set-url", "origin", str(remote / "unavailable"))
    assert G.n1_tag() == commits[1]
    getattr(G.n1_tag, "cache_clear", lambda: None)()
    monkeypatch.setattr(G, "FORK_RELEASE_FLOOR", "f" * 40)
    with pytest.raises(RuntimeError, match="fork.*N-1|N-1.*fork"):
        G.n1_tag()
