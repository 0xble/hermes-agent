"""Regression coverage for the live-main drop guard."""

from __future__ import annotations

import argparse
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd, update_receipt
from hermes_cli.subcommands.update import build_update_parser
from hermes_cli.update_main_guard import MainDrop, MainDropCheckSkipped, find_main_drop


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "state").write_text("A\n", encoding="utf-8")
    git(root, "add", "state")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "state").write_text("B\n", encoding="utf-8")
    git(root, "commit", "-qam", "merged semaphore fix")
    main_one = git(root, "rev-parse", "HEAD")
    (root / "state").write_text("C\n", encoding="utf-8")
    git(root, "commit", "-qam", "merged gateway fix")
    main_tip = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", main_tip)
    return SimpleNamespace(root=root, base=base, main_one=main_one, main_tip=main_tip)


def runner(root):
    def run(args, **kwargs):
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
            check=kwargs.pop("check", False), **kwargs
        )

    return run


def test_refuses_target_that_drops_live_main_commits(history):
    finding = find_main_drop(runner(history.root), history.base, history.main_tip)

    assert finding is not None
    assert finding.count == 2
    assert "would drop 2 commits" in finding.message
    assert "merged gateway fix" in finding.message
    assert "merged semaphore fix" in finding.message
    assert "Merge origin/main into the candidate and retry." in finding.message


def test_allows_descendant_of_live_release(history):
    git(history.root, "checkout", "-qb", "candidate", history.main_tip)
    (history.root / "state").write_text("candidate\n", encoding="utf-8")
    git(history.root, "commit", "-qam", "candidate descendant")
    candidate = git(history.root, "rev-parse", "HEAD")

    assert find_main_drop(runner(history.root), candidate, history.main_tip) is None


def test_allows_origin_main_itself(history):
    assert find_main_drop(runner(history.root), "origin/main", history.main_one) is None


def test_missing_origin_main_is_reported_as_a_skipped_check(history):
    def no_main(args, **kwargs):
        result = runner(history.root)(args, **kwargs)
        if args[:2] == ["rev-parse", "--verify"] and args[2] == "origin/main^{commit}":
            result.stdout = ""
        return result

    with pytest.raises(MainDropCheckSkipped, match="origin/main"):
        find_main_drop(no_main, history.base, history.main_tip)


def test_allow_drop_main_is_recorded_in_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda args: None)
    args = parser.parse_args(["update", "--allow-drop-main"])
    assert args.allow_drop_main is True

    update_receipt.begin_update_receipt()
    update_cmd._guard_against_main_drop(args, ["git"], "target", target_is_head=False)
    path = update_receipt.finalize_update_receipt("success")
    assert path is not None
    data = path.read_text(encoding="utf-8")
    assert '"allow_drop_main": true' in data


def test_post_swap_argv_does_not_forward_main_drop_override():
    args = SimpleNamespace(
        gateway=False, no_backup=False, backup=False, yes=False, keep_stash=False,
        switch_branch=False, allow_drop_main=True, force=False, force_venv=False,
        no_gateway_restart=False, branch=None,
    )
    assert "--allow-drop-main" not in update_cmd._post_swap_argv_tail(args)


def test_main_drop_refusal_uses_exit_two_and_closed_stop_class(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    args = SimpleNamespace(allow_drop_main=False)
    finding = MainDrop("target-sha", "live-sha", "main-sha", (("deadbeef", "live fix"),))
    monkeypatch.setattr(update_cmd, "_live_release_sha", lambda *_: "live-sha")
    monkeypatch.setattr("hermes_cli.update_main_guard.find_main_drop", lambda *a, **k: finding)

    update_receipt.begin_update_receipt()
    with pytest.raises(SystemExit) as exited:
        update_cmd._guard_against_main_drop(args, ["git"], "target", target_is_head=False)

    assert exited.value.code == 2
    path = tmp_path / "home" / "logs" / "update_receipts" / "latest.json"
    assert path.is_file()
    assert '"stop_class": "main_drop"' in path.read_text(encoding="utf-8")


def _update_parser():
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda args: None)
    return parser


def test_expect_sha_parses_only_an_exact_lowercase_commit(capsys):
    parser = _update_parser()
    assert parser.parse_args(["update", "--branch", "x", "--expect-sha", "a" * 40]).expect_sha == "a" * 40
    assert parser.parse_args(["update"]).expect_sha is None
    for bad in ("A" * 40, "a" * 39, "a" * 41, "abc123", "main", "g" * 40, ""):
        with pytest.raises(SystemExit) as refused:
            parser.parse_args(["update", "--branch", "x", "--expect-sha", bad])
        assert refused.value.code == 2
    assert "40-hex" in capsys.readouterr().err


@pytest.mark.parametrize("other", [{}, {"check": True}, {"plan": True}, {"rollback": True}])
def test_expect_sha_without_a_fetching_branch_update_is_refused(capsys, other):
    from hermes_cli import main as hermes_main

    args = SimpleNamespace(branch=None if not other else "x", expect_sha="a" * 40, **other)
    with pytest.raises(SystemExit) as refused:
        hermes_main._update_preflight_handled(args)
    assert refused.value.code == 2
    assert "--expect-sha" in capsys.readouterr().out


def test_post_swap_argv_keeps_the_pin_with_its_branch_only():
    base = dict(gateway=False, no_backup=False, backup=False, yes=False, keep_stash=False,
                switch_branch=False, force=False, force_venv=False, no_gateway_restart=False)
    pinned = SimpleNamespace(**base, branch="release", expect_sha="b" * 40)
    assert update_cmd._post_swap_argv_tail(pinned) == ["--branch", "release", "--expect-sha", "b" * 40]
    assert update_cmd._post_swap_argv_tail(SimpleNamespace(**base, branch="release", expect_sha=None)) == [
        "--branch", "release"]
    assert update_cmd._post_swap_argv_tail(SimpleNamespace(**base, branch=None, expect_sha=None)) == []
