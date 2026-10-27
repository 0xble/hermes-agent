"""Live candidates survive fetch-only immutable updates without touching the checkout.

Use real Git history, archives, release markers and pointer activation. Only the
build dependencies and fresh-interpreter/service boundary are bounded locally.
"""

import argparse
import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import immutable_releases as releases, main, update_cmd, update_receipt
from hermes_cli import update_cmd_candidate as candidates
from hermes_cli.subcommands.update import build_update_parser

_real_landing_status = candidates.candidate_landing_status

# Every subprocess is confined to the fixture's throwaway repository and home.
pytestmark = pytest.mark.live_system_guard_bypass


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Update Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    (source / "hermes_cli").mkdir()
    (source / "hermes_cli" / "immutable_releases.py").write_text("# fixture\n")
    (source / "shared.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    git(source, "switch", "-c", "candidate")
    (source / "candidate.txt").write_text("installed optimistic change\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "candidate")
    live = git(source, "rev-parse", "HEAD")
    git(source, "switch", "main")
    (source / "target.txt").write_text("new main change\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "target")
    target = git(source, "rev-parse", "HEAD")
    git(source, "update-ref", "refs/remotes/origin/main", target)
    git(source, "fetch", ".", "main")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The forge boundary is injected, while Git, staging and activation stay real.
    monkeypatch.setattr(candidates, "candidate_landing_status", lambda *a, **kw: "unmerged")
    monkeypatch.setattr(main, "PROJECT_ROOT", source)
    monkeypatch.setattr(releases, "_build_candidate_web", lambda *_: None)
    monkeypatch.setattr(releases, "prepare_venv", lambda *a, **kw: None)
    monkeypatch.setattr(releases, "smoke_plugins", lambda *a, **kw: None)
    monkeypatch.setattr(releases, "_relocate_venv", lambda *a: None)
    previous, _ = releases.stage_release(source, home, sha=live)
    releases.activate_release(home, previous)
    monkeypatch.setattr(update_cmd, "_write_fleet_restart_pending_marker", lambda **kw: None)
    handed = []

    def handoff(args, **payload):
        from hermes_cli.immutable_update_handoff import detach_update_receipt
        from hermes_cli.update_completion import _resume_receipt

        # Exercise the real receipt transfer, bounding only the child/service run.
        _resume_receipt(json.loads(json.dumps(detach_update_receipt())))
        handed.append(payload["release"])
        releases.activate_release(home, payload["release"])
        update_receipt.persist_running_receipt()

    monkeypatch.setattr(update_cmd, "_hand_off_post_swap", handoff)
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
    with update_receipt.update_receipt_scope():
        update_receipt.begin_update_receipt()
        receipt = update_receipt._current.get()
        yield SimpleNamespace(source=source, home=home, base=base, live=live,
                              target=target, parser=parser, handed=handed, receipt=receipt)


def apply(installed, *flags):
    args = installed.parser.parse_args(["update", "--yes", *flags])
    update_cmd._apply_fetched_immutable_update(
        ["git"], "main", SimpleNamespace(no_gateway_restart=False), args,
        gateway_mode=False, had_desktop_app_before_update=False,
        pre_update_snapshot_id=None, _pre_update_plan=None, _windows_gateway_resume=None,
    )
    active = releases.read_pointer(releases.ReleasePaths.for_home(installed.home).current)
    assert active is not None
    return active


def test_fetch_update_carries_live_candidate_into_activated_tree(installed, capsys):
    before = git(installed.source, "rev-parse", "HEAD")
    active = apply(installed)
    assert (active / "candidate.txt").read_text() == "installed optimistic change\n"
    assert (active / "target.txt").read_text() == "new main change\n"
    assert git(installed.source, "show", "-s", "--format=%P", active.name).split() == [
        installed.target, installed.live,
    ]
    assert git(installed.source, "rev-parse", "HEAD") == before
    assert git(installed.source, "status", "--porcelain") == ""
    assert git(installed.source, "branch", "--contains", active.name) == ""
    assert installed.live in capsys.readouterr().out
    assert update_receipt._current.get().data["carried_candidates"] == [installed.live]
    persisted = json.loads(next((installed.home / "logs" / "update_receipts").glob("update_*.json")).read_text())
    assert persisted["carried_candidates"] == [installed.live]
    assert persisted["candidate_update_target"] == installed.target
    assert git(installed.source, "rev-parse", f"refs/hermes/carry-forward/{active.name}") == active.name
    # An unchanged remote must not create a new synthetic commit or restart again.
    installed.handed.clear()
    assert apply(installed) == active
    assert installed.handed == []


def commit_file(root, filename, body):
    (root / filename).write_text(body)
    git(root, "add", filename)
    git(root, "commit", "-m", filename)
    return git(root, "rev-parse", "HEAD")


def change_live(installed, sha):
    previous, _ = releases.stage_release(installed.source, installed.home, sha=sha)
    releases.activate_release(installed.home, previous)
    installed.live = sha


def test_conflict_refuses_without_activation_and_records_stop(installed, capsys):
    git(installed.source, "switch", "candidate")
    live = commit_file(installed.source, "shared.txt", "candidate edit\n")
    change_live(installed, live)
    git(installed.source, "switch", "main")
    target = commit_file(installed.source, "shared.txt", "main edit\n")
    git(installed.source, "update-ref", "refs/remotes/origin/main", target)
    git(installed.source, "fetch", ".", "main")
    with pytest.raises(SystemExit) as stopped:
        apply(installed)
    assert stopped.value.code == 2
    assert installed.handed == []
    assert releases.read_pointer(installed.home / "current").name == live
    assert git(installed.source, "rev-parse", "HEAD") == target
    assert git(installed.source, "status", "--porcelain") == ""
    out = capsys.readouterr().out
    assert live in out and "--replace-candidate" in out and "conflict" in out.lower()
    assert update_receipt._current.get().data["stop_class"] == "merge_conflict"


def test_explicit_replace_drops_only_named_candidate(installed):
    active = apply(installed, "--replace-candidate", installed.live)
    assert active.name == installed.target
    assert not (active / "candidate.txt").exists()
    assert update_receipt._current.get().data["replaced_candidate"] == installed.live
    args = installed.parser.parse_args(["update", "--replace-candidate", installed.live])
    # The post-swap child runs the target's parser, which may predate the flag.
    assert update_cmd._post_swap_argv_tail(args) == []


@pytest.mark.parametrize("replacement", ["wrong", "short"])
def test_replacement_must_name_exact_live_candidate(installed, replacement):
    value = {"wrong": installed.base, "short": installed.live[:12]}[replacement]
    with pytest.raises(SystemExit):
        apply(installed, "--replace-candidate", value)
    assert installed.handed == []
    assert releases.read_pointer(installed.home / "current").name == installed.live


@pytest.mark.parametrize("already_in", ["main", "target"])
def test_merged_or_contained_live_keeps_bare_target(installed, already_in):
    if already_in == "main":
        # The target can be an older release. Existing main-drop policy, not
        # carry-forward, owns permission to roll back merged main changes.
        git(installed.source, "update-ref", "refs/remotes/origin/main", installed.live)
    else:
        git(installed.source, "merge", "--no-edit", "candidate")
        installed.target = git(installed.source, "rev-parse", "HEAD")
        git(installed.source, "fetch", ".", "main")
    active = apply(installed)
    assert active.name == installed.target
    assert "carried_candidates" not in update_receipt._current.get().data


@pytest.mark.parametrize("landing", ["squash", "merge", "squash-deleted-branch"])
def test_carried_candidate_landing_returns_to_published_target(installed, landing, monkeypatch):
    combined = apply(installed)
    # Carry over a second main advance too, so synthetic ancestry cannot leak in.
    commit_file(installed.source, "later.txt", "later main change\n")
    git(installed.source, "fetch", ".", "main")
    combined = apply(installed)
    if landing.startswith("squash"):
        git(installed.source, "merge", "--squash", "candidate")
        git(installed.source, "commit", "-m", "squash candidate")
        if landing == "squash-deleted-branch":
            git(installed.source, "branch", "-D", "candidate")
    else:
        git(installed.source, "merge", "--no-edit", "candidate")
    target = git(installed.source, "rev-parse", "HEAD")
    git(installed.source, "update-ref", "refs/remotes/origin/main", target)
    git(installed.source, "fetch", ".", "main")
    monkeypatch.setattr(candidates, "candidate_landing_status", lambda *a, **kw: "merged")
    assert apply(installed).name == target
    assert (releases.read_pointer(installed.home / "previous")).name == combined.name
    assert git(installed.source, "status", "--porcelain") == ""


@pytest.mark.parametrize("commits,landing", [(1, "squash"), (2, "squash"), (2, "cherry-pick")])
@pytest.mark.parametrize("followup", ["revert", "delete", "edit"])
def test_squash_landed_candidate_respects_later_main_changes(installed, commits, landing, followup, capsys, monkeypatch):
    if commits == 2:
        git(installed.source, "switch", "candidate")
        live = commit_file(installed.source, "candidate.txt", "reviewed candidate content\n")
        change_live(installed, live)
        git(installed.source, "switch", "main")
    # Leave the named candidate branch pointing at the installed commit, as on
    # repositories that retain branches after a squash merge.
    if landing == "squash":
        git(installed.source, "merge", "--squash", "candidate")
        git(installed.source, "commit", "-m", "squash candidate")
        landed = [git(installed.source, "rev-parse", "HEAD")]
    else:
        candidate_commits = git(installed.source, "rev-list", "--reverse", f"{installed.base}..candidate").split()
        landed = []
        for sha in candidate_commits:
            git(installed.source, "cherry-pick", sha)
            landed.append(git(installed.source, "rev-parse", "HEAD"))
    if followup == "revert":
        git(installed.source, "revert", "--no-edit", *reversed(landed))
    elif followup == "delete":
        git(installed.source, "rm", "candidate.txt")
        git(installed.source, "commit", "-m", "remove landed candidate")
    else:
        commit_file(installed.source, "candidate.txt", "main supersedes candidate\n")
    target = git(installed.source, "rev-parse", "HEAD")
    git(installed.source, "update-ref", "refs/remotes/origin/main", target)
    git(installed.source, "fetch", ".", "main")
    monkeypatch.setattr(candidates, "candidate_landing_status", lambda *a, **kw: "merged")
    active = apply(installed)
    assert active.name == target
    if followup == "edit":
        assert (active / "candidate.txt").read_text() == "main supersedes candidate\n"
    else:
        assert not (active / "candidate.txt").exists()
    assert "CARRYING FORWARD" not in capsys.readouterr().out
    assert "carried_candidates" not in update_receipt._current.get().data
    assert git(installed.source, "rev-parse", "HEAD") == target
    assert git(installed.source, "status", "--porcelain") == ""


@pytest.mark.parametrize("commits", [1, 2])
def test_unlanded_candidate_is_not_mistaken_for_target_history(installed, commits):
    if commits == 2:
        git(installed.source, "switch", "candidate")
        live = commit_file(installed.source, "candidate.txt", "reviewed candidate content\n")
        change_live(installed, live)
        git(installed.source, "switch", "main")
    # Similar text in an unrelated path must not count as a landed patch.
    commit_file(installed.source, "other.txt", "installed optimistic change\n")
    target = git(installed.source, "rev-parse", "HEAD")
    git(installed.source, "update-ref", "refs/remotes/origin/main", target)
    git(installed.source, "fetch", ".", "main")
    active = apply(installed)
    assert active.name != target
    expected = "reviewed candidate content\n" if commits == 2 else "installed optimistic change\n"
    assert (active / "candidate.txt").read_text() == expected
    assert (active / "other.txt").read_text() == "installed optimistic change\n"
    assert git(installed.source, "rev-parse", f"{active.name}^2") == installed.live


@pytest.mark.parametrize("followup", ["revert", "delete", "edit"])
def test_nearby_context_squash_landing_is_authoritative(installed, monkeypatch, followup, capsys):
    root = installed.source
    lines = [f"line{i}\n" for i in range(1, 21)]
    # Shared history with real adjacent lines, not an untouched candidate-only file.
    git(root, "switch", "--detach", installed.base)
    base = commit_file(root, "nearby.txt", "".join(lines))
    git(root, "switch", "-C", "candidate", base)
    lines[9] = "candidate-change\n"
    live = commit_file(root, "nearby.txt", "".join(lines))
    change_live(installed, live)
    git(root, "switch", "-C", "main", base)
    lines[9] = "line10\n"
    lines[7] = "main-nearby-change\n"
    commit_file(root, "nearby.txt", "".join(lines))
    git(root, "merge", "--squash", "candidate")
    git(root, "commit", "-m", "squash candidate against changed context")
    landing = git(root, "rev-parse", "HEAD")
    if followup == "revert":
        git(root, "revert", "--no-edit", landing)
    elif followup == "delete":
        git(root, "rm", "nearby.txt")
        git(root, "commit", "-m", "delete landed content")
    else:
        lines[9] = "superseding-main-change\n"
        commit_file(root, "nearby.txt", "".join(lines))
    target = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", target)
    git(root, "fetch", ".", "main")
    monkeypatch.setattr(candidates, "candidate_landing_status", lambda *a, **kw: "merged")
    active = apply(installed)
    assert active.name == target
    if followup == "delete":
        assert not (active / "nearby.txt").exists()
    else:
        expected = "line10\n" if followup == "revert" else "superseding-main-change\n"
        assert (active / "nearby.txt").read_text().splitlines(keepends=True)[9] == expected
    assert "CARRYING FORWARD" not in capsys.readouterr().out
    assert "carried_candidates" not in update_receipt._current.get().data
    assert git(root, "rev-parse", "HEAD") == target
    assert git(root, "status", "--porcelain") == ""



@pytest.mark.parametrize("origin", [
    "https://github.com/example/hermes-agent.git",
    "git@github.com:example/hermes-agent.git",
    "ssh://git@github.com/example/hermes-agent.git",
])
@pytest.mark.parametrize("state", ["merged", "unmerged", "unknown"])
def test_landing_evidence_uses_origin_pr_record(installed, monkeypatch, origin, state):
    from hermes_cli import source_check

    git(installed.source, "remote", "add", "origin", origin)
    pr = {"base": {"ref": "main", "repo": {"full_name": "example/hermes-agent"}},
          "head": {"sha": installed.live}, "state": "open", "merged_at": None}
    if state == "merged":
        pr.update(state="closed", merged_at="2026-10-24T12:00:00Z", merge_commit_sha=installed.target)
    urls = []

    def request(url):
        urls.append(url)
        return json.dumps([] if state == "unknown" else [pr])

    monkeypatch.setattr(source_check, "_request", request)
    assert _real_landing_status(installed.source, installed.live, base_branches={"main"},
                                target=installed.target) == state
    assert urls == [f"https://api.github.com/repos/example/hermes-agent/commits/{installed.live}/pulls?per_page=100&page=1"]


@pytest.mark.parametrize("case", ["offline", "malformed", "no-pr", "closed", "moved-head", "other-base", "other-repo",
                                  "non-github", "no-origin", "merged-outside-target", "merged-unknown-commit"])
def test_uncertain_landing_evidence_is_unknown(installed, monkeypatch, case):
    from hermes_cli import source_check

    pr = {"base": {"ref": "main", "repo": {"full_name": "example/hermes-agent"}},
          "head": {"sha": installed.live}, "state": "open", "merged_at": None}
    if case != "no-origin":
        origin = "https://example.com/example/hermes-agent.git" if case == "non-github" else "https://github.com/example/hermes-agent.git"
        git(installed.source, "remote", "add", "origin", origin)
    mutate = {
        "closed": lambda: pr.update(state="closed"),
        "moved-head": lambda: pr["head"].update(sha=installed.target),
        "other-base": lambda: pr["base"].update(ref="unrelated"),
        "other-repo": lambda: pr["base"]["repo"].update(full_name="other/hermes-agent"),
        # Merged elsewhere (or after this fetch): returning the bare target would drop it.
        "merged-outside-target": lambda: pr.update(
            state="closed", merged_at="2026-10-24T12:00:00Z", merge_commit_sha=installed.live),
        "merged-unknown-commit": lambda: pr.update(
            state="closed", merged_at="2026-10-24T12:00:00Z", merge_commit_sha="f" * 40),
    }
    mutate.get(case, lambda: None)()

    def request(url):
        if case == "offline":
            raise OSError("offline")
        if case == "malformed":
            return '{"message": "unavailable"}'
        return json.dumps([] if case == "no-pr" else [pr])

    monkeypatch.setattr(source_check, "_request", request)
    assert _real_landing_status(installed.source, installed.live, base_branches={"main"},
                                target=installed.target) == "unknown"


@pytest.mark.parametrize("last_page", ["merged", "failure", "empty", "incomplete"])
def test_landing_evidence_reads_all_pages_before_carry(installed, monkeypatch, last_page):
    from hermes_cli import source_check

    git(installed.source, "remote", "add", "origin", "https://github.com/example/hermes-agent.git")
    pr = {"base": {"ref": "stable", "repo": {"full_name": "example/hermes-agent"}},
          "head": {"sha": installed.live}, "state": "open", "merged_at": None}
    urls = []

    def request(url):
        urls.append(url)
        if len(urls) == 1 or last_page == "incomplete":
            return json.dumps([pr] * 100)
        if last_page == "failure":
            raise OSError("page unavailable")
        if last_page == "empty":
            return "[]"
        return json.dumps([{**pr, "state": "closed", "merged_at": "2026-10-24T12:00:00Z",
                            "merge_commit_sha": installed.target}])

    monkeypatch.setattr(source_check, "_request", request)
    expected = {"merged": "merged", "failure": "unknown", "empty": "unmerged", "incomplete": "unknown"}[last_page]
    assert _real_landing_status(installed.source, installed.live, base_branches={"main", "stable"},
                                target=installed.target) == expected
    assert len(urls) == (10 if last_page == "incomplete" else 2)


def test_unknown_landing_refuses_without_dropping_or_carrying(installed, monkeypatch, capsys):
    monkeypatch.setattr(candidates, "candidate_landing_status", lambda *a, **kw: "unknown")
    with pytest.raises(SystemExit) as stopped:
        apply(installed)
    assert stopped.value.code == 2
    assert releases.read_pointer(installed.home / "current").name == installed.live
    assert installed.handed == []
    out = capsys.readouterr().out
    assert "landing" in out.lower()
    assert f"--replace-candidate {installed.live}" in out
    assert "carried_candidates" not in update_receipt._current.get().data
    assert apply(installed, "--replace-candidate", installed.live).name == installed.target


def test_uppercase_replacement_names_the_same_commit(installed):
    assert apply(installed, "--replace-candidate", installed.live.upper()).name == installed.target
    assert update_receipt._current.get().data["replaced_candidate"] == installed.live


@pytest.mark.parametrize("carried", [False, True])
@pytest.mark.parametrize("revision", ["amend", "rebase"])
def test_rewritten_named_candidate_refuses_stale_resurrection(installed, capsys, carried, revision):
    # Receipts from the existing updater retain argv and exact release transition.
    receipts = installed.home / "logs" / "update_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "update_original.json").write_text(json.dumps({
        "argv": ["hermes", "update", "--branch", "candidate"],
        "release_transition": {"to_sha": installed.live}, "started_at": "2026-10-23",
    }))
    git(installed.source, "remote", "add", "origin", ".")
    active = apply(installed) if carried else releases.read_pointer(installed.home / "current")
    if carried:
        assert json.loads((active / "install-stamp.json").read_text())["sourceBranch"] == "candidate"
        (receipts / "update_original.json").unlink()
    git(installed.source, "switch", "candidate")
    git(installed.source, "reset", "--hard", installed.base if revision == "amend" else installed.target)
    reviewed = commit_file(installed.source, "reviewed.txt", "replacement without removed candidate code\n")
    git(installed.source, "switch", "main")
    git(installed.source, "merge", "--no-edit", "candidate")
    target = git(installed.source, "rev-parse", "HEAD")
    git(installed.source, "fetch", ".", "main")
    # An exact head refresh must not rely on the stale remote tracking branch.
    git(installed.source, "update-ref", "refs/remotes/origin/candidate", installed.live)
    installed.handed.clear()
    with pytest.raises(SystemExit) as stopped:
        apply(installed)
    assert stopped.value.code == 2
    assert releases.read_pointer(installed.home / "current") == active
    assert installed.handed == []
    out = capsys.readouterr().out
    assert installed.live in out and reviewed in out and "--replace-candidate" in out
    assert apply(installed, "--replace-candidate", active.name).name == target
    assert not (releases.read_pointer(installed.home / "current") / "candidate.txt").exists()


def test_old_carry_receipt_does_not_misidentify_target_branch_as_candidate(installed):
    receipts = installed.home / "logs" / "update_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "update_original.json").write_text(json.dumps({
        "argv": ["hermes", "update", "--branch", "candidate"],
        "release_transition": {"to_sha": installed.live},
    }))
    first = releases.merge_release_commits(installed.source, installed.target, installed.live)
    combined = releases.merge_release_commits(installed.source, installed.target, first)
    staged, _ = releases.stage_release(installed.source, installed.home, sha=combined)
    releases.activate_release(installed.home, staged)
    git(installed.source, "update-ref", "-d", f"refs/hermes/carry-forward/{first}")
    (receipts / "update_old_carry.json").write_text(json.dumps({
        "argv": ["hermes", "update", "--branch", "main"],
        "release_transition": {"to_sha": combined}, "carried_candidates": [installed.live],
    }))
    # Newest-first scanning meets unreadable receipts first; they are not provenance.
    (receipts / "update_zz_corrupt.json").write_text("{")
    (receipts / "update_zz_shape.json").write_text(json.dumps({"release_transition": "unexpected"}))
    git(installed.source, "remote", "add", "origin", ".")
    commit_file(installed.source, "next.txt", "next target\n")
    git(installed.source, "fetch", ".", "main")
    selected = apply(installed)
    assert selected.name != combined
    assert update_receipt._current.get().data["carried_candidates"] == [installed.live]
    assert json.loads((selected / "install-stamp.json").read_text())["sourceBranch"] == "candidate"
    assert git(installed.source, "rev-parse", f"{selected.name}^2") == installed.live


def test_activation_prunes_unneeded_carry_refs_but_retains_rollback(installed):
    (installed.home / "release-layout.json").write_text(json.dumps({"source": str(installed.source)}))
    first = apply(installed)
    commit_file(installed.source, "later.txt", "second target\n")
    git(installed.source, "fetch", ".", "main")
    second = apply(installed)
    commit_file(installed.source, "later.txt", "third target\n")
    git(installed.source, "fetch", ".", "main")
    third = apply(installed)
    refs = git(installed.source, "for-each-ref", "--format=%(objectname)", "refs/hermes/carry-forward/").split()
    assert set(refs) == {second.name, third.name}
    assert first.name not in refs
    releases.activate_release(installed.home, second, operation="rollback")
    assert set(git(installed.source, "for-each-ref", "--format=%(objectname)", "refs/hermes/carry-forward/").split()) == {second.name, third.name}


def test_failed_ref_prune_still_finishes_activation(installed, monkeypatch, capsys):
    (installed.home / "release-layout.json").write_text(json.dumps({"source": str(installed.source)}))

    def locked(*_):
        raise subprocess.CalledProcessError(128, ["git", "update-ref"], stderr="ref lock held")

    monkeypatch.setattr(releases, "prune_carry_forward_refs", locked)
    active = apply(installed)
    assert releases.read_pointer(installed.home / "current") == active
    assert not (installed.home / "release-txn.json").exists()
    assert "carry-forward refs" in capsys.readouterr().err
