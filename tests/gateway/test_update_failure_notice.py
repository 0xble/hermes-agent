"""Failed updates explain the cause without claiming unproved runtime changes."""
import json
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.update_notifications import _failure_cause, final_outcome
from tests.gateway.update_fixtures import (
    RELEASE, SOURCE, _make_runner, _release, fail_immutable_update, finalize_update, immutable_source, pending,
)
from tests.hermes_cli.test_update_target_identity import update_tree  # fixture


@pytest.fixture(autouse=True)
def notification_checkout(tmp_path, monkeypatch):
    """Keep recovery-state probes in a synthetic linked checkout, not the running tree."""
    root = tmp_path / "source"
    root.mkdir()
    git_dir = tmp_path / "git" / "worktrees" / "source"
    git_dir.mkdir(parents=True)
    (root / ".git").write_text(f"gitdir: {git_dir}\n")
    monkeypatch.setattr("gateway.update_notifications.__file__", str(root / "gateway" / "update_notifications.py"))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "host-state"))
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("immutable", [False, True])
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("stop_reason", [
    "\n  candidate plugin smoke failed on plugin rea.  \nTraceback (most recent call last):\n  File \"probe.py\", line 1\nRuntimeError: smoke failed",
    "x" * 1000,
    "Traceback (most recent call last):\n  File \"probe.py\", line 1\nRuntimeError: smoke failed",
    None,
    "sys.exit(1)",
])
async def test_failed_update_delivers_unchanged_version_and_one_line_cause(
    tmp_path, immutable, exit_code, stop_reason,
):
    from tests.hermes_cli.test_update_target_identity import git

    data = pending(tmp_path, reason=False)
    data["updating_notified"] = True
    (tmp_path / ".update_pending.json").write_text(json.dumps(data))
    expected_sha = SOURCE
    if immutable:
        source = immutable_source(tmp_path)
        expected_sha = git(source, "rev-parse", "HEAD")
        _release(tmp_path, sha=expected_sha)
        fail_immutable_update(tmp_path, source)
    else:
        finalize_update(tmp_path, outcome="failed", noop=True)
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt.update(stop_reason=stop_reason, stop_class="syntax_rollback")
    path.write_text(json.dumps(receipt))
    (tmp_path / ".update_process_exit_code").write_text(str(exit_code))
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}

    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification()

    text = adapter.send.call_args.args[1]
    assert text.startswith("❌ Update Failed\n\nNothing changed. Still on ")
    assert expected_sha[:12] in text
    assert "Cause:" in text and len(text.splitlines()[-1]) < 400
    assert "Traceback" not in text and "RuntimeError" not in text
    assert "unverified" not in text
    if stop_reason and "candidate plugin" in stop_reason:
        assert "Cause: candidate plugin smoke failed on plugin rea." in text
    if stop_reason == "sys.exit(1)":
        assert "Cause: The downloaded update contains a syntax error." in text
        assert "sys.exit" not in text
    assert not (tmp_path / ".update_pending.json").exists()


@pytest.mark.parametrize("local_work", ["clean", "edits", "lockfile-churn"])
def test_fetch_failure_proves_unchanged_only_before_source_cleanup(
    update_tree, tmp_path, monkeypatch, local_work,
):
    from hermes_cli import update_cmd, update_receipt
    from tests.hermes_cli.test_update_target_identity import git

    source = update_tree.clone
    git(source, "checkout", "-q", "main")
    if local_work == "lockfile-churn":
        (source / "package-lock.json").write_text("{}\n")
        git(source, "add", "package-lock.json")
        git(source, "-c", "commit.gpgsign=false", "commit", "-qm", "lockfile")
        (source / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    elif local_work == "edits":
        (source / "content.txt").write_text("local edits\n")
    sha = git(source, "rev-parse", "HEAD")
    before = git(source, "diff")
    home = tmp_path / "home"
    home.mkdir()
    request = pending(home, reason=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: home / "logs/update_receipts")
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"sha": git(source, "rev-parse", "HEAD")})
    monkeypatch.setattr("gateway.update_notifications.__file__", str(source / "gateway/update_notifications.py"))
    update_tree.args.channel = "main"
    run_git = update_cmd._git_run

    def fail_fetch(cmd, args, *a, **kw):
        if args[0] == "fetch":
            return subprocess.CompletedProcess([*cmd, *args], 1, stdout="", stderr="fatal: fetch failed")
        return run_git(cmd, args, *a, **kw)

    monkeypatch.setattr(update_cmd, "_git_run", fail_fetch)
    with update_receipt.update_receipt_scope():
        with pytest.raises(SystemExit) as exc:
            update_cmd._cmd_update_impl(update_tree.args, gateway_mode=False)
        assert exc.value.code == 1
        path = update_receipt.finalize_update_receipt("failed", stop_reason="sys.exit(1)")
    assert path is not None
    receipt = json.loads(path.read_text())
    assert receipt["pre_update"]["sha"] == receipt["post_update"]["sha"] == sha
    assert receipt["stop_class"] == "fetch_failed"
    assert git(source, "rev-parse", "HEAD") == sha
    (home / ".update_process_exit_code").write_text("1")
    outcome = final_outcome(home, request)
    assert outcome is not None
    success, detail = outcome
    assert success is False
    assert "Cause: fetch_failed" in detail
    if local_work == "lockfile-churn":
        assert git(source, "diff") != before
        assert "Nothing changed" not in detail
        assert "Runtime state is unverified" in detail
        assert "Local changes" not in detail
    else:
        assert git(source, "diff") == before
        assert detail == f"Nothing changed. Still on {sha[:12]}. Cause: fetch_failed"


def test_git_fallback_recovery_invalidates_earlier_unchanged_proof(update_tree, tmp_path, monkeypatch):
    from hermes_cli import update_cmd, update_receipt
    from hermes_cli._early_recovery_zip import write_zip_swap_journal, zip_entry_identity
    from tests.hermes_cli.test_update_target_identity import git

    source = update_tree.clone
    git(source, "checkout", "-q", "main")
    sha = git(source, "rev-parse", "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    request = pending(home, reason=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: home / "logs/update_receipts")
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"sha": git(source, "rev-parse", "HEAD")})
    monkeypatch.setattr("gateway.update_notifications.__file__", str(source / "gateway/update_notifications.py"))
    update_tree.args.channel = "main"
    before = (source / "content.txt").read_text()

    def fail_git(*_a, **_kw):
        # A previous swap's installed bytes happen to match HEAD. Its backup does not.
        backup = source / "content.txt.hermes-update-old"
        backup.write_text("source before interrupted swap\n")
        write_zip_swap_journal(source, "swapping", [[
            "content.txt", True, zip_entry_identity(source / "content.txt"), zip_entry_identity(backup),
        ]], "0123456789ab")
        raise subprocess.CalledProcessError(1, ["git", "status"])

    monkeypatch.setattr(update_cmd._check, "report_pack_tidy", fail_git)
    monkeypatch.setattr(update_cmd, "_should_zip_fallback_on_update_error", lambda _: True)
    with update_receipt.update_receipt_scope():
        with pytest.raises(SystemExit) as exc:
            update_cmd._cmd_update_impl(update_tree.args, gateway_mode=False)
        assert exc.value.code == 1
        path = update_receipt.finalize_update_receipt("failed", stop_reason="sys.exit(1)")
    assert path is not None
    receipt = json.loads(path.read_text())
    assert receipt["pre_update"]["sha"] == receipt["post_update"]["sha"] == sha
    assert receipt["stop_class"] == "local_changes_blocked"
    steps = {step["name"]: step for step in receipt["steps"]}
    assert steps["local_changes_stash"]["ok"] is True
    assert steps["local_changes_stash"]["detail"].startswith("not_needed: ")
    assert steps["zip_source_swap"]["ok"] is False
    assert "interrupted ZIP swap recovery" in steps["zip_source_swap"]["detail"]
    assert (source / "content.txt").read_text() != before
    assert (source / "content.txt").read_text() == "source before interrupted swap\n"
    (home / ".update_process_exit_code").write_text("1")
    success, detail = final_outcome(home, request)
    assert success is False
    assert "Nothing changed" not in detail
    assert "Runtime state is unverified" in detail


@pytest.mark.parametrize("failure", ["direct-zip", "fallback-zip", "refusal", "branch-name"])
def test_pre_stash_failure_never_claims_restoration_or_unchanged_zip(
    update_tree, tmp_path, monkeypatch, failure,
):
    from hermes_cli import update_cmd, update_receipt
    from tests.hermes_cli.test_update_target_identity import git

    source = update_tree.clone
    git(source, "checkout", "-q", "main")
    sha = git(source, "rev-parse", "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    request = pending(home, reason=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: home / "logs/update_receipts")
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"sha": git(source, "rev-parse", "HEAD")})
    monkeypatch.setattr("gateway.update_notifications.__file__", str(source / "gateway/update_notifications.py"))
    update_tree.args.channel = "main"

    def fail(*_a, **_kw):
        raise RuntimeError("checkout failed before stash")

    def fail_zip(*_a, **_kw):
        # A failed swap can leave mixed files without changing .git HEAD.
        (source / "content.txt").write_text("partially swapped source\n")
        raise RuntimeError("ZIP swap rollback failed")

    if failure == "direct-zip":
        prepare = update_cmd._prepare_git_command

        def select_zip(**kw):
            _, cmd, fork = prepare(**kw)
            return True, cmd, fork

        monkeypatch.setattr(update_cmd, "_prepare_git_command", select_zip)
    elif failure == "fallback-zip":
        def fail_git(*_a, **_kw):
            raise subprocess.CalledProcessError(1, ["git", "status"])

        monkeypatch.setattr(update_cmd._check, "report_pack_tidy", fail_git)
        monkeypatch.setattr(update_cmd, "_should_zip_fallback_on_update_error", lambda _: True)
    elif failure == "refusal":
        monkeypatch.setattr(update_cmd, "_refuse_before_commit_point", fail)
    else:
        monkeypatch.setattr(update_cmd, "_current_branch_name", fail)
    monkeypatch.setattr("hermes_cli.update_cmd_zip._download_and_swap_zip", fail_zip)

    with update_receipt.update_receipt_scope():
        with pytest.raises(RuntimeError, match="ZIP swap rollback failed|checkout failed before stash"):
            update_cmd._cmd_update_impl(update_tree.args, gateway_mode=False)
        path = update_receipt.finalize_update_receipt("failed", stop_reason="injected update failure")
    assert path is not None
    receipt = json.loads(path.read_text())
    assert receipt["pre_update"]["sha"] == receipt["post_update"]["sha"] == sha
    assert git(source, "stash", "list") == ""
    assert bool(git(source, "diff")) == failure.endswith("zip")
    (home / ".update_process_exit_code").write_text("1")
    outcome = final_outcome(home, request)
    assert outcome is not None
    success, detail = outcome
    assert success is False
    assert "Cause: injected update failure" in detail
    assert "Nothing changed" not in detail
    assert "Runtime state is unverified" in detail
    assert "Local changes" not in detail


@pytest.mark.parametrize("route", ["direct", "fallback"])
@pytest.mark.parametrize("result", ["success", "swap-failed", "completion-failed", "parked-failed"])
def test_zip_receipt_preserves_stash_disposition_and_settles_before_completion(
    update_tree, tmp_path, monkeypatch, route, result,
):
    from copy import deepcopy
    from hermes_cli import update_cmd, update_cmd_stash, update_receipt
    from tests.hermes_cli.test_update_target_identity import git

    source = update_tree.clone
    git(source, "checkout", "-q", "main")
    sha = git(source, "rev-parse", "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    request = pending(home, reason=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: home / "logs/update_receipts")
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"sha": git(source, "rev-parse", "HEAD")})
    monkeypatch.setattr("gateway.update_notifications.__file__", str(source / "gateway/update_notifications.py"))
    update_tree.args.channel = "main"
    stash = None
    handoff_steps = []

    def choose_park():
        nonlocal stash
        if result == "parked-failed":
            (source / "content.txt").write_text("local edits\n")
            stash = update_cmd._stash_local_changes_if_needed(["git"], source)
            assert stash is not None
            assert not update_cmd._restore_stashed_changes(
                ["git"], source, stash, prompt_user=True, input_fn=lambda *_: "n")
            assert update_cmd_stash._pending_autostash is None

    if route == "direct":
        prepare = update_cmd._prepare_git_command

        def select_zip(**kw):
            _, cmd, fork = prepare(**kw)
            choose_park()
            return True, cmd, fork

        monkeypatch.setattr(update_cmd, "_prepare_git_command", select_zip)
    else:
        def fail_git(*_a, **_kw):
            choose_park()
            raise subprocess.CalledProcessError(1, ["git", "status"])

        monkeypatch.setattr(update_cmd, "_refuse_before_commit_point", fail_git)
        monkeypatch.setattr(update_cmd, "_should_zip_fallback_on_update_error", lambda _: True)

    def swap(*_a, **_kw):
        if stash:
            assert git(source, "rev-parse", "refs/stash") == stash
        (source / "content.txt").write_text("ZIP source without changing git HEAD\n")
        if result in {"swap-failed", "parked-failed"}:
            raise RuntimeError("ZIP swap failed")
        return update_tree.newer

    def complete(_request):
        current = update_receipt._current.get()
        assert current is not None
        handoff_steps.extend(deepcopy(current.data["steps"]))
        assert next(s for s in handoff_steps if s["name"] == "zip_source_swap")["ok"] is True
        if result == "completion-failed":
            raise RuntimeError("completion failed")

    monkeypatch.setattr("hermes_cli.update_cmd_zip._download_and_swap_zip", swap)
    monkeypatch.setattr(update_cmd, "_complete_source_update", complete)
    with update_receipt.update_receipt_scope():
        if result == "success":
            update_cmd._cmd_update_impl(update_tree.args, gateway_mode=False)
        else:
            with pytest.raises(RuntimeError, match="ZIP swap failed|completion failed"):
                update_cmd._cmd_update_impl(update_tree.args, gateway_mode=False)
        path = update_receipt.finalize_update_receipt(
            "success" if result == "success" else "failed", stop_reason="injected ZIP failure")
    assert path is not None
    receipt = json.loads(path.read_text())
    assert receipt["pre_update"]["sha"] == receipt["post_update"]["sha"] == sha
    stash_steps = [s for s in receipt["steps"] if s["name"] == "local_changes_stash"]
    if result == "parked-failed":
        assert [s["detail"] for s in stash_steps] == [f"parked: {stash} (restore declined)"]
    elif result in {"success", "completion-failed"}:
        assert all(s["ok"] is True for s in stash_steps)
        assert all(s["ok"] is True for s in handoff_steps if s["name"] == "local_changes_stash")
    if result != "success":
        (home / ".update_process_exit_code").write_text("1")
        outcome = final_outcome(home, request)
        assert outcome is not None
        success, detail = outcome
        assert success is False
        assert "Nothing changed" not in detail
        assert "Runtime state is unverified" in detail
        if stash:
            assert f"Local changes remain in stash {stash}" in detail
            assert f"git stash apply {stash}" in detail


@pytest.mark.parametrize("dirty", [False, True])
def test_immutable_build_failure_has_untouched_source_proof(update_tree, tmp_path, monkeypatch, dirty):
    from tests.hermes_cli.test_update_target_identity import git

    home = tmp_path / "home"
    home.mkdir()
    release = _release(home, sha=update_tree.base)
    source = update_tree.clone
    if dirty:
        (source / "content.txt").write_text("local edits\n")
    before = git(source, "status", "--porcelain")
    request = pending(home, reason=False)
    monkeypatch.setattr("gateway.update_notifications.__file__", str(release / "gateway/update_notifications.py"))
    path = fail_immutable_update(home, source)
    receipt = json.loads(path.read_text())
    assert not any(step["name"] == "local_changes_stash" for step in receipt["steps"])
    assert git(source, "status", "--porcelain") == before
    assert git(source, "rev-parse", "HEAD") == update_tree.base
    assert final_outcome(home, request) == (
        False, f"Nothing changed. Still on {release.name[:12]}. Cause: candidate build failed",
    )
    receipt["steps"].append({"name": "immutable_source_untouched", "ok": False})
    latest = home / "logs/update_receipts/latest.json"
    latest.write_text(json.dumps(receipt))
    assert "Runtime state is unverified" in final_outcome(home, request)[1]
    # The pointer and equal source SHAs alone must never create this assurance.
    receipt["steps"] = []
    latest.write_text(json.dumps(receipt))
    assert "Runtime state is unverified" in final_outcome(home, request)[1]


@pytest.mark.parametrize("case", [
    "missing", "malformed", "stale", "bad_timestamp", "unfinished", "changed", "missing_sha", "invalid_sha",
    "restart", "fleet", "transition", "release_pending", "fleet_pending", "success_receipt", "broken_release",
    "missing_working_tree_fact",
])
def test_failed_exit_never_claims_unchanged_without_current_receipt_proof(tmp_path, case):
    data = pending(tmp_path, reason=False)
    finalize_update(tmp_path, outcome="failed", noop=True)
    (tmp_path / ".update_process_exit_code").write_text("7")
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt["stop_reason"] = "candidate plugin smoke failed on plugin rea."
    if case == "missing":
        path.unlink()
    elif case == "malformed":
        path.write_text("{")
    else:
        if case == "missing_working_tree_fact":
            receipt["steps"] = []
        elif case == "stale":
            receipt["started_at"] = receipt["finished_at"] = (
                datetime.now(UTC) - timedelta(days=1)
            ).isoformat()
        elif case == "bad_timestamp":
            receipt["finished_at"] = "not a timestamp"
        elif case == "unfinished":
            receipt.pop("finished_at")
        elif case == "changed":
            receipt["post_update"]["sha"] = "b" * 40
        elif case == "missing_sha":
            receipt["post_update"] = {}
        elif case == "invalid_sha":
            receipt["pre_update"] = receipt["post_update"] = {"sha": "not-a-sha"}
        elif case == "broken_release":
            (tmp_path / "current").symlink_to(tmp_path / "missing-release")
        elif case == "restart":
            receipt["gateway_restart"] = {"incomplete": True}
        elif case == "fleet":
            receipt["fleet"] = [{"state": "restart_pending"}]
        elif case == "transition":
            _release(tmp_path)
            receipt["release_transition"] = {"from_sha": SOURCE, "to_sha": RELEASE}
        elif case == "release_pending":
            (tmp_path / "release-txn.json").write_text("{}")
        elif case == "fleet_pending":
            (tmp_path / "fleet_restart_pending").write_text("pending")
        elif case == "success_receipt":
            receipt["outcome"] = "success"
        path.write_text(json.dumps(receipt))

    ok, detail = final_outcome(tmp_path, data)
    assert ok is False
    assert "Runtime state is unverified" in detail
    assert "Nothing changed" not in detail and "Still on" not in detail
    if case in {"changed", "missing_sha", "restart", "fleet", "transition", "release_pending", "fleet_pending"}:
        assert "Cause: candidate plugin smoke failed on plugin rea." in detail


def test_failed_update_without_a_source_checkout_keeps_state_unverified(tmp_path, notification_checkout):
    # ZIP/non-git installs cannot prove the absence of source-keyed recovery debt.
    (notification_checkout / ".git").unlink()
    request = pending(tmp_path, reason=False)
    finalize_update(tmp_path, outcome="failed", noop=True)
    (tmp_path / ".update_process_exit_code").write_text("1")

    success, detail = final_outcome(tmp_path, request)

    assert success is False
    assert "Nothing changed" not in detail
    assert "Runtime state is unverified" in detail


@pytest.mark.parametrize("stop_class, expected", [
    ("syntax_rollback", "The downloaded update contains a syntax error."),
    ("target_syntax_error", "The selected update contains a syntax error."),
    ("future_stop_class", "future_stop_class"),
    (None, "The updater exited with code 1."),
])
@pytest.mark.parametrize("reason", ["sys.exit(1)", "\n  sys.exit(130)  \n", None])
def test_boundary_exit_reason_falls_back_to_stop_class(stop_class, expected, reason):
    assert _failure_cause({"stop_reason": reason, "stop_class": stop_class}, 1) == expected


def test_boundary_exit_reason_preserves_restart_explanation():
    receipt = {"stop_reason": "sys.exit(1)", "stop_class": "syntax_rollback",
               "gateway_restart": {"phase_error": "The gateway could not restart."}}
    assert _failure_cause(receipt, 1) == "The gateway could not restart."


@pytest.mark.parametrize("marker", ["interrupted_pull", "completion", "host", "legacy_host"])
def test_matching_head_does_not_prove_an_unchanged_checkout_with_recovery_owed(
    tmp_path, notification_checkout, marker,
):
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.update_host_obligation import HOST_OBLIGATION_NAME, host_obligation_path
    from hermes_cli.venv_sync import completion_pending_path

    data = pending(tmp_path, reason=False)
    finalize_update(tmp_path, outcome="failed", noop=True)
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt.update(stop_reason="sys.exit(1)", stop_class="syntax_rollback")
    path.write_text(json.dumps(receipt))
    (tmp_path / ".update_process_exit_code").write_text("1")
    markers = {
        "interrupted_pull": interrupted_pull_marker(notification_checkout),
        "completion": completion_pending_path(notification_checkout),
        "host": host_obligation_path(notification_checkout),
        "legacy_host": host_obligation_path(notification_checkout).with_name(HOST_OBLIGATION_NAME),
    }
    owed = markers[marker]
    owed.parent.mkdir(parents=True, exist_ok=True)
    owed.write_text("unreadable recovery record")

    ok, detail = final_outcome(tmp_path, data)
    assert ok is False
    assert detail == ("Cause: The downloaded update contains a syntax error. "
                      "Runtime state is unverified; see the update output.")
    assert "Nothing changed" not in detail and "Still on" not in detail

    owed.unlink()
    assert final_outcome(tmp_path, data) == (
        False, "Nothing changed. Still on aaaaaaaaaaaa. Cause: The downloaded update contains a syntax error.",
    )


@pytest.mark.parametrize("marker", ["interrupted_pull", "completion", "host", "legacy_host"])
def test_immutable_notice_checks_recovery_in_journal_bound_source(
    tmp_path, notification_checkout, monkeypatch, marker,
):
    from hermes_cli import update_host_obligation as obligation
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.venv_sync import completion_pending_path
    from tests.hermes_cli.test_update_target_identity import git

    # Start fresh rather than reinitializing the autouse fixture's synthetic gitdir.
    # A real source identity keeps the immutable resolver's git validation in the test.
    source = immutable_source(tmp_path)
    release = _release(tmp_path, sha=git(source, "rev-parse", "HEAD"))
    (tmp_path / "release-layout.json").write_text(json.dumps({"source": str(source)}))
    monkeypatch.setattr("gateway.update_notifications.__file__", str(release / "gateway" / "update_notifications.py"))
    # The updater runs from the source, while the gateway imports its release copy.
    monkeypatch.setattr(obligation, "__file__", str(source / "hermes_cli" / "update_host_obligation.py"))
    data = pending(tmp_path, reason=False)
    fail_immutable_update(tmp_path, source)
    markers = {
        "interrupted_pull": interrupted_pull_marker(source),
        "completion": completion_pending_path(source),
        "host": obligation.host_obligation_path(),
        "legacy_host": obligation.host_obligation_path().with_name(obligation.HOST_OBLIGATION_NAME),
    }
    owed = markers[marker]
    if marker == "host":
        assert obligation.write_host_obligation(expected_sha=release.name)
        assert owed.is_file()
        assert not (tmp_path / "fleet_restart_pending").exists()
        assert not markers["legacy_host"].exists()
    else:
        owed.parent.mkdir(parents=True, exist_ok=True)
        owed.write_text("unreadable recovery record")
    # Model module locations in the gateway process, after the source updater exits.
    monkeypatch.setattr(obligation, "__file__", str(release / "hermes_cli" / "update_host_obligation.py"))
    assert obligation.host_obligation_path() != markers["host"]

    ok, detail = final_outcome(tmp_path, data)
    assert ok is False
    assert "Runtime state is unverified" in detail
    assert "Nothing changed" not in detail and "Still on" not in detail

    owed.unlink()
    assert final_outcome(tmp_path, data) == (
        False, f"Nothing changed. Still on {release.name[:12]}. Cause: candidate build failed",
    )
