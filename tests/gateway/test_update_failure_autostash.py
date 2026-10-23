"""Failed pulls must disclose local edits parked outside the working tree."""
import json
import subprocess

import pytest

from gateway.update_notifications import final_outcome
from tests.gateway.update_fixtures import pending
from tests.hermes_cli.test_update_target_identity import git, update_tree  # noqa: F401


@pytest.mark.parametrize("failure", ["commit_point", "syntax_rollback"])
@pytest.mark.parametrize("dirty", [False, True])
def test_failed_pull_notice_preserves_local_work_evidence(update_tree, tmp_path, monkeypatch, failure, dirty):
    from hermes_cli import main, update_cmd, update_cmd_stash, update_host_obligation, update_receipt
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.venv_sync import completion_pending_path

    tree = update_tree
    git(tree.clone, "switch", "-q", "main")
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"sha": git(tree.clone, "rev-parse", "HEAD")})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "host-state"))
    monkeypatch.setattr("gateway.update_notifications.__file__", str(tree.clone / "gateway/update_notifications.py"))
    monkeypatch.setattr(update_host_obligation, "__file__", str(tree.clone / "hermes_cli/update_host_obligation.py"))
    request = pending(home, reason=False)
    local = tree.clone / "content.txt"
    if dirty:
        local.write_text("local edits\n", encoding="utf-8")
    if failure == "commit_point":
        monkeypatch.setattr(update_cmd._commit, "arm_commit_point", lambda *_a, **_k: "fixture commit-point refusal")
    else:
        # The selected commit is syntactically broken. Exercise the real tree move,
        # syntax check, rollback and recovery-marker cleanup, not a receipt stand-in.
        (tree.origin / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        git(tree.origin, "add", "broken.py")
        git(tree.origin, "-c", "commit.gpgsign=false", "commit", "-qm", "broken target")
        monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_FILES", ("broken.py",))
    git(tree.clone, "fetch", "-q", "origin")
    update_receipt.begin_update_receipt()
    try:
        stash = main._stash_local_changes_if_needed(["git"], tree.clone)
        with pytest.raises(SystemExit) as error:
            update_cmd._pull_updates(
                ["git"], "main", stash, prompt_for_restore=False, gw_input_fn=None,
                discard_local_changes=False, keep_stash=False,
            )
        assert error.value.code == 1
    finally:
        update_receipt.finalize_update_receipt("failed")
        update_cmd_stash._clear_pending_autostash()
    (home / ".update_process_exit_code").write_text("1", encoding="utf-8")
    assert git(tree.clone, "rev-parse", "HEAD") == tree.base
    assert not interrupted_pull_marker(tree.clone).exists()
    assert not completion_pending_path(tree.clone).exists()
    assert not update_host_obligation.host_obligation_path(tree.clone).exists()
    receipt = json.loads((home / "logs/update_receipts/latest.json").read_text())
    assert receipt["stop_class"] == ("commit_point_refused" if failure == "commit_point" else "syntax_rollback")
    assert receipt["pre_update"]["sha"] == receipt["post_update"]["sha"] == tree.base
    ok, detail = final_outcome(home, request)
    assert ok is False
    assert "Cause:" in detail
    if dirty:
        assert local.read_text() == "base\n"
        assert git(tree.clone, "show", f"{stash}:content.txt") == "local edits"
        assert "Nothing changed" not in detail
        assert "Local changes remain in stash" in detail
        assert stash in detail
    else:
        assert detail.startswith(f"Nothing changed. Still on {tree.base[:12]}. Cause:")


@pytest.mark.parametrize("failure", ["upstream_target", "already_current_restore", "branch_restore", "torn_switch"])
def test_pre_pull_failure_notice_names_parked_autostash(update_tree, tmp_path, monkeypatch, failure):
    from hermes_cli import main, update_cmd, update_cmd_stash, update_host_obligation, update_receipt
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.venv_sync import completion_pending_path

    tree = update_tree
    git(tree.clone, "switch", "-q", "main")
    git(tree.origin, "reset", "--hard", tree.base)
    git(tree.clone, "fetch", "-q", "origin")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "host-state"))
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"sha": git(tree.clone, "rev-parse", "HEAD")})
    monkeypatch.setattr("gateway.update_notifications.__file__", str(tree.clone / "gateway/update_notifications.py"))
    monkeypatch.setattr(update_host_obligation, "__file__", str(tree.clone / "hermes_cli/update_host_obligation.py"))
    # Import health is an external probe. Syntax validation, stash, apply, rejection
    # cleanup, upstream rollback and receipt persistence remain real.
    monkeypatch.setattr(update_cmd, "_critical_module_import_failures", lambda *_a, **_k: {})
    request = pending(home, reason=False)
    local = tree.clone / "content.txt"
    local.write_text("local edits\n", encoding="utf-8")
    unsafe = tree.clone / "unsafe.py"
    if failure in {"already_current_restore", "branch_restore"}:
        unsafe.write_text("def broken(:\n", encoding="utf-8")

    def broken_upstream(*_a, **_k):
        raise update_cmd.UpstreamTargetBroken(tree.newer, "upstream.py", "invalid syntax")

    monkeypatch.setattr(main, "_sync_with_upstream_if_needed", broken_upstream)
    update_receipt.begin_update_receipt()
    try:
        with pytest.raises(SystemExit) as error:
            if failure in {"upstream_target", "already_current_restore"}:
                plan = update_cmd._prepare_checkout_for_update(
                    ["git"], "main", "main", is_fork=failure == "upstream_target",
                    assume_yes=True, gateway_mode=True, gw_input_fn=None,
                    switch_branch=False, _windows_gateway_resume=None,
                )
                update_cmd._finish_already_up_to_date(
                    ["git"], "main", "main", plan, gw_input_fn=None, completion_request=None,
                )
            else:
                stash = main._stash_local_changes_if_needed(["git"], tree.clone)
                if failure == "torn_switch":
                    update_cmd._commit.arm_tree_move(
                        ["git"], tree.clone, pre=tree.base, target=tree.newer, stash=stash,
                    )
                result = subprocess.CompletedProcess(update_cmd._ARM_REFUSED, 1, "", "fixture switch refusal")
                update_cmd._exit_after_failed_branch_switch(["git"], "main", result, stash, None)
        assert error.value.code == 1
    finally:
        update_receipt.finalize_update_receipt("failed")
        update_cmd_stash._clear_pending_autostash()
    (home / ".update_process_exit_code").write_text("1", encoding="utf-8")

    stash = git(tree.clone, "rev-parse", "refs/stash")
    assert git(tree.clone, "show", f"{stash}:content.txt") == "local edits"
    assert local.read_text() == "base\n"
    assert not unsafe.exists()
    assert git(tree.clone, "rev-parse", "HEAD") == tree.base
    assert interrupted_pull_marker(tree.clone).exists() == (failure == "torn_switch")
    assert not completion_pending_path(tree.clone).exists()
    assert not update_host_obligation.host_obligation_path(tree.clone).exists()
    receipt = json.loads((home / "logs/update_receipts/latest.json").read_text())
    assert receipt["pre_update"]["sha"] == receipt["post_update"]["sha"] == tree.base
    ok, detail = final_outcome(home, request)
    assert ok is False and "Cause:" in detail
    assert "Nothing changed" not in detail
    assert f"Local changes remain in stash {stash}" in detail
    assert f"git stash apply {stash}" in detail


@pytest.mark.parametrize("evidence", ["user_action", "parked", "restored", "discarded"])
def test_failure_notice_honors_recorded_local_work_disposition(tmp_path, monkeypatch, evidence):
    from tests.gateway.update_fixtures import finalize_update

    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    monkeypatch.setattr("gateway.update_notifications.__file__", str(source / "gateway/update_notifications.py"))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "host-state"))
    request = pending(tmp_path, reason=False)
    finalize_update(tmp_path, outcome="failed", noop=True)
    path = tmp_path / "logs/update_receipts/latest.json"
    receipt = json.loads(path.read_text())
    receipt["stop_reason"] = "plugin smoke failed"
    if evidence == "user_action":
        receipt["user_action"] = {"step": "local_changes_stash", "reason": "Restore local changes before retrying."}
    else:
        receipt["steps"] = [{"name": "local_changes_stash", "ok": evidence != "parked",
                             "detail": f"{evidence}: {'b' * 40} (fixture)"}]
    path.write_text(json.dumps(receipt))
    (tmp_path / ".update_process_exit_code").write_text("1")

    ok, detail = final_outcome(tmp_path, request)
    assert ok is False and "Cause: plugin smoke failed" in detail
    if evidence == "restored":
        assert detail.startswith("Nothing changed. Still on ")
        return
    assert "Nothing changed" not in detail
    if evidence == "parked":
        assert "Local changes remain in stash" in detail and "b" * 40 in detail
    assert "Runtime state is unverified" in detail
