"""An update refuses a target that would drop commits the live release runs.

Incident (2026-10-13 18:36): a ``request_update`` from one session installed main ``5b944834`` while
the live release ran the unmerged ``fix/post-update-resume`` branch (PR #567), silently dropping
that fix for seven minutes. The live release SHA must be an ancestor of (or patch-equivalent to
commits in) the target; otherwise the updater names the dropped commits and their branch/PR and
stops before staging anything. ``--allow-drop-live`` is the explicit rollback override.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import update_live_guard as guard


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo: Path, name: str, subject: str) -> str:
    (repo / name).write_text(subject + "\n", encoding="utf-8")
    _git(repo, "add", name)
    # The subject goes through a file, not argv: the hermetic live-system guard treats any argv
    # holding a bare ``update`` token under a ``hermes``-named tmp dir as ``hermes update``.
    message = repo.parent / f".msg-{name}"
    message.write_text(subject + "\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-F", str(message))
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """main: m1 -> m2.  fix/post-update-resume: m1 -> x1 -> x2 (unmerged)."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    m1 = _commit(repo, "base.txt", "base")
    _git(repo, "checkout", "-q", "-b", "fix/post-update-resume")
    x1 = _commit(repo, "resume.txt", "fix(gateway): resume interrupted turns after update")
    x2 = _commit(repo, "resume2.txt", "test(gateway): cover post-update resume")
    _git(repo, "checkout", "-q", "main")
    m2 = _commit(repo, "main.txt", "Merge pull request #573 from 0xble/fix/telegram-evidence-toggles")
    _git(repo, "update-ref", "refs/remotes/origin/fix/post-update-resume", x2)
    _git(repo, "update-ref", "refs/remotes/origin/main", m2)
    return SimpleNamespace(path=repo, m1=m1, m2=m2, x1=x1, x2=x2)


def _runner(repo: Path):
    return lambda args: subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def test_target_that_drops_live_branch_commits_is_refused_naming_branch_and_pr(repo, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    guard.record_release_branch(home, repo.x2, "fix/post-update-resume")
    message = guard.refusal(_runner(repo.path), home, live=repo.x2, target=repo.m2,
                            pr_lookup=lambda branch: {"number": 567, "state": "OPEN", "headRefOid": repo.x2,
                                                      "url": "https://github.com/0xble/hermes-agent/pull/567"}
                            if branch == "fix/post-update-resume" else None)
    assert message is not None
    assert "fix/post-update-resume" in message
    assert "PR #567" in message
    assert "2 commit" in message
    assert "resume interrupted turns after update" in message
    assert "cover post-update resume" in message
    assert repo.m2[:12] in message and repo.x2[:12] in message
    assert "--allow-drop-live" in message
    # The refusal is the only guidance a gateway session gets: an explicit --branch main is
    # not consent, so it must name the remedy (merge the PR, or roll back from a terminal).
    assert "Merge PR #567 first" in message
    assert "explicit --branch does not count as consent" in message
    assert "from a terminal" in message


def test_descendant_branch_install_passes(repo, tmp_path):
    _git(repo.path, "checkout", "-q", "fix/post-update-resume")
    _git(repo.path, "merge", "-q", "--no-edit", "main")
    merged = _git(repo.path, "rev-parse", "HEAD")
    assert guard.refusal(_runner(repo.path), tmp_path, live=repo.x2, target=merged,
                         pr_lookup=lambda _b: None) is None


def test_forward_main_update_passes(repo, tmp_path):
    assert guard.refusal(_runner(repo.path), tmp_path, live=repo.m1, target=repo.m2,
                         pr_lookup=lambda _b: None) is None


def test_cherry_picked_live_commits_do_not_count_as_dropped(repo, tmp_path):
    _git(repo.path, "cherry-pick", repo.x1, repo.x2)
    picked = _git(repo.path, "rev-parse", "HEAD")
    assert guard.refusal(_runner(repo.path), tmp_path, live=repo.x2, target=picked,
                         pr_lookup=lambda _b: None) is None


def _squash_merge_into_main(repo) -> str:
    """Squash-merge the live branch into main, the way this repo lands most PRs."""
    _git(repo.path, "checkout", "-q", "main")
    _git(repo.path, "merge", "-q", "--squash", "fix/post-update-resume")
    message = repo.path.parent / ".msg-squash"
    message.write_text("fix(gateway): resume interrupted turns (#567)\n", encoding="utf-8")
    _git(repo.path, "commit", "-q", "-F", str(message))
    return _git(repo.path, "rev-parse", "HEAD")


def test_multi_commit_branch_squash_merged_into_target_passes(repo, tmp_path):
    """Per-commit patch-ids miss a squash of x1+x2; the combined change is already in main."""
    squashed = _squash_merge_into_main(repo)
    later = _commit(repo.path, "later.txt", "chore: unrelated follow-up on main")
    for target in (squashed, later):
        assert guard.refusal(_runner(repo.path), tmp_path, live=repo.x2, target=target,
                             pr_lookup=lambda _b: None) is None


def test_squash_merged_then_edited_on_target_passes_when_the_pr_is_merged(repo, tmp_path):
    """A follow-up on main that rewrites the squashed lines defeats the tree check; a MERGED PR
    whose head contains the live commit and whose merge commit the target contains still proves
    nothing is dropped."""
    squashed = _squash_merge_into_main(repo)
    edited = _commit(repo.path, "resume.txt", "fix(gateway): rework resume after #567")
    merged = {"number": 567, "state": "MERGED", "url": "u", "headRefOid": repo.x2,
              "mergeCommit": {"oid": squashed}}

    def refused(pr, target=edited):
        return guard.refusal(_runner(repo.path), tmp_path, live=repo.x2, target=target,
                             pr_lookup=lambda _b: pr) is not None

    assert not refused(merged)
    assert refused({**merged, "state": "OPEN"})
    assert refused({**merged, "headRefOid": repo.x1})  # live has commits pushed after the merged head
    assert refused(merged, target=repo.m2)  # rollback to a main that predates the merge
    assert refused(None)


def test_partially_squashed_branch_is_still_refused(repo, tmp_path):
    """Only x1's change landed; x2 is still dropped."""
    _git(repo.path, "checkout", "-q", "main")
    _git(repo.path, "checkout", repo.x1, "--", "resume.txt")
    message = repo.path.parent / ".msg-partial"
    message.write_text("partial\n", encoding="utf-8")
    _git(repo.path, "commit", "-q", "-F", str(message))
    partial = _git(repo.path, "rev-parse", "HEAD")
    assert guard.refusal(_runner(repo.path), tmp_path, live=repo.x2, target=partial,
                         pr_lookup=lambda _b: None) is not None


def test_unrecorded_live_release_names_the_remote_branch_that_contains_it(repo, tmp_path):
    seen = []
    message = guard.refusal(_runner(repo.path), tmp_path, live=repo.x2, target=repo.m2,
                            pr_lookup=lambda branch: seen.append(branch) or None)
    assert message is not None and "fix/post-update-resume" in message
    assert seen == ["fix/post-update-resume"]


def test_unknown_live_commit_does_not_block_updates(repo, tmp_path):
    """A pruned live commit cannot be proven dropped; failing closed would block every update."""
    assert guard.refusal(_runner(repo.path), tmp_path, live="f" * 40, target=repo.m2,
                         pr_lookup=lambda _b: None) is None


def test_branch_records_are_bounded_and_latest_wins(tmp_path):
    for index in range(guard.MAX_BRANCH_RECORDS + 5):
        guard.record_release_branch(tmp_path, f"{index:040x}", f"b{index}")
    guard.record_release_branch(tmp_path, f"{0:040x}", "again")
    data = json.loads((tmp_path / guard.BRANCHES_FILE).read_text(encoding="utf-8"))
    assert len(data) == guard.MAX_BRANCH_RECORDS
    assert guard.recorded_branch(tmp_path, f"{0:040x}") == "again"


# ---- updater integration -------------------------------------------------------------------------

@pytest.fixture
def immutable(repo, tmp_path, monkeypatch):
    from hermes_cli import main, update_cmd

    home = tmp_path / "home"
    live = home / "releases" / repo.x2
    live.mkdir(parents=True)
    for name in (".release-ready", ".hermes_build_sha"):
        (live / name).write_text(repo.x2 + "\n", encoding="utf-8")
    (home / "current").symlink_to(live)
    monkeypatch.setattr(main, "PROJECT_ROOT", repo.path)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    staged = []

    def fake_stage(source, stage_home, *, sha):
        staged.append(sha)
        raise SystemExit("staged")

    monkeypatch.setattr("hermes_cli.immutable_releases.stage_release", fake_stage)
    monkeypatch.setattr(guard, "lookup_pr", lambda *_a, **_k: None)

    def run(target: str, *, allow: bool = False, branch: str = "main"):
        (repo.path / ".git" / "FETCH_HEAD").write_text(f"{target}\t\tbranch '{branch}' of origin\n")
        with pytest.raises(SystemExit) as exited:
            update_cmd._apply_fetched_immutable_update(
                ["git"], branch, SimpleNamespace(no_gateway_restart=False),
                SimpleNamespace(allow_drop_live=allow), gateway_mode=True,
                had_desktop_app_before_update=False, pre_update_snapshot_id=None,
                _pre_update_plan=None, _windows_gateway_resume=None)
        return exited.value.code

    return SimpleNamespace(run=run, staged=staged, home=home)


def test_updater_refuses_before_staging_when_target_drops_live_commits(repo, immutable, capsys):
    assert immutable.run(repo.m2) == 1
    assert immutable.staged == []
    out = capsys.readouterr().out
    assert "fix/post-update-resume" in out and "resume interrupted turns after update" in out


def test_allow_drop_live_override_stages_an_intentional_rollback(repo, immutable):
    assert immutable.run(repo.m2, allow=True) == "staged"
    assert immutable.staged == [repo.m2]


def test_descendant_target_stages(repo, immutable):
    _git(repo.path, "checkout", "-q", "fix/post-update-resume")
    newer = _commit(repo.path, "more.txt", "fix: follow-up on the same branch")
    assert immutable.run(newer, branch="fix/post-update-resume") == "staged"
    assert immutable.staged == [newer]


def test_updater_records_the_branch_it_installs(repo, immutable, monkeypatch):
    from hermes_cli import update_cmd

    monkeypatch.setattr("hermes_cli.immutable_releases.stage_release",
                        lambda source, home, *, sha: (home / "releases" / sha, "staged"))
    monkeypatch.setattr("hermes_cli.immutable_releases._release_is_ready", lambda path, sha: True)
    monkeypatch.setattr(update_cmd, "_write_fleet_restart_pending_marker", lambda **_k: True)
    monkeypatch.setattr(update_cmd, "_hand_off_post_swap", lambda *_a, **_k: (_ for _ in ()).throw(SystemExit(0)))
    _git(repo.path, "checkout", "-q", "fix/post-update-resume")
    newer = _commit(repo.path, "more.txt", "fix: follow-up on the same branch")
    assert immutable.run(newer, branch="fix/post-update-resume") == 0
    assert guard.recorded_branch(immutable.home, newer) == "fix/post-update-resume"


def test_cli_parser_exposes_the_override():
    import argparse
    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda _a: None)
    assert parser.parse_args(["update", "--allow-drop-live"]).allow_drop_live is True
    assert parser.parse_args(["update"]).allow_drop_live is False
