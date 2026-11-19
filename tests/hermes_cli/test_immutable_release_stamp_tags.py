"""An immutable release's install stamp names the version its commit shipped under.

A source checkout that never fetched the release tag describes its commit against an older tag, so
the stamp named 0.20.0 for a 0.23.1 release and every plugin gated on a newer ``requires_hermes``
was skipped. Staging fetches the missing release tags first and refuses a version that went backwards.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases

_GIT = ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*_GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, version: str, note: str) -> None:
    (repo / "hermes_cli").mkdir(exist_ok=True)
    (repo / "hermes_cli" / "immutable_releases.py").write_text("# staging capability probe\n")
    (repo / "pyproject.toml").write_text(f"[project]\nname='probe'\nversion='{version}'\n")
    (repo / "note.txt").write_text(note)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", note)


@pytest.fixture
def history(tmp_path):
    """Official repo: calendar tag v2026.8.3 (0.20.0), then v0.23.1 one commit before HEAD. The fork
    has the same commits and only the calendar tag, as the live update source did."""
    official = tmp_path / "official"
    official.mkdir()
    _git(official, "init", "-q", "--initial-branch=main")
    _commit(official, "0.20.0", "calendar release")
    _git(official, "tag", "v2026.8.3")
    _commit(official, "0.23.1", "between")
    _commit(official, "0.23.1", "release")
    _git(official, "tag", "v0.23.1")
    _commit(official, "0.23.1", "after release")
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(official), str(fork)], check=True)
    _git(fork, "tag", "-d", "v0.23.1")
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(fork), str(source)], check=True)
    assert _git(source, "tag", "--list").split() == ["v2026.8.3"]
    return official, source


@pytest.fixture(autouse=True)
def _no_network(tmp_path, monkeypatch):
    """Tests never reach the real official repository; one that wants it names a local stand-in."""
    _official_url(monkeypatch, tmp_path / "missing-remote")


@pytest.fixture
def stage(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(releases, "prepare_venv", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *args, **kwargs: None)

    def run(source: Path, live_base_version: str | None = None):
        if live_base_version:
            live = home / "releases" / "live"
            live.mkdir(parents=True)
            (live / "install-stamp.json").write_text(json.dumps({"baseVersion": live_base_version}))
            os.symlink(live, home / "current")
        release, _ = releases.stage_release(source, home)
        return json.loads((release / "install-stamp.json").read_text())

    return home, run


def _official_url(monkeypatch, url: Path) -> None:
    from hermes_cli import update_cmd_git

    monkeypatch.setattr(update_cmd_git, "OFFICIAL_REPO_URL", str(url))


def test_missing_release_tag_is_fetched_before_stamping(history, stage, monkeypatch):
    official, source = history
    _official_url(monkeypatch, official)

    stamp = stage[1](source, live_base_version="0.23.0")

    assert (stamp["baseVersion"], stamp["distance"]) == ("0.23.1", 1)
    assert "v0.23.1" in _git(source, "tag", "--list").split()


def test_stamp_reads_base_version_and_distance_when_tag_is_present(history, stage):
    official, source = history
    _git(source, "fetch", "-q", "--no-tags", str(official), "refs/tags/v0.23.1:refs/tags/v0.23.1")

    stamp = stage[1](source, live_base_version="0.23.1")

    assert (stamp["baseVersion"], stamp["distance"], stamp["displayVersion"]) == ("0.23.1", 1, "0.23.1+1")


def test_unreachable_tag_remotes_refuse_instead_of_stamping_the_old_version(history, stage):
    _, source = history

    with pytest.raises(RuntimeError, match=r"refusing to stamp.*0\.20\.0.*older than the live release's 0\.23\.0.*tag fetch failed"):
        stage[1](source, live_base_version="0.23.0")

    assert not list((stage[0] / "releases").glob("*/install-stamp.json")) or [
        path.parent.name for path in (stage[0] / "releases").glob("*/install-stamp.json")] == ["live"]
    assert not any(path.name.startswith(".staging-") for path in (stage[0] / "releases").iterdir())


def test_version_regression_against_live_stamp_is_refused_with_tags_present(history, stage):
    official, source = history
    _git(source, "fetch", "-q", "--no-tags", str(official), "refs/tags/v0.23.1:refs/tags/v0.23.1")

    with pytest.raises(RuntimeError, match=r"0\.23\.1 is older than the live release's 0\.24\.0"):
        stage[1](source, live_base_version="0.24.0")


def test_unknown_version_is_a_regression_from_a_stamped_live_release(tmp_path, stage):
    source = tmp_path / "tagless"
    source.mkdir()
    _git(source, "init", "-q")
    _commit(source, "0.1.0", "no release tags")

    with pytest.raises(RuntimeError, match=r"base version unknown is older than the live release's 0\.23\.0"):
        stage[1](source, live_base_version="0.23.0")


def test_first_stage_without_live_release_has_nothing_to_regress_from(history, stage):
    _, source = history

    assert stage[1](source)["baseVersion"] == "0.20.0"
