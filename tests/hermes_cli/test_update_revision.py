"""Immutable ``hermes update --revision`` boundaries use a real temporary Git remote."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import update_revision


def _git(args, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def _git_run(_git_cmd, args, cwd, *, network=False, check=False):
    del network
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _repo(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", str(remote)], tmp_path)
    seed = tmp_path / "seed"
    _git(["clone", str(remote), str(seed)], tmp_path)
    _git(["config", "user.email", "test@example.com"], seed)
    _git(["config", "user.name", "Test"], seed)
    for name in ('hermes_cli/main.py', 'gateway/run.py', 'run_agent.py'):
        file = seed / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('READY = True\n')
    (seed / "pyproject.toml").write_text("[project]\nname = 'pin-test'\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "first"], seed)
    _git(["push", "origin", "HEAD:main"], seed)
    first = _git(["rev-parse", "HEAD"], seed).stdout.strip()
    checkout = tmp_path / "checkout"
    _git(["clone", str(remote), str(checkout)], tmp_path)
    _git(["checkout", "main"], checkout)
    return seed, checkout, first


def _advance(seed: Path) -> str:
    (seed / "after.txt").write_text("later\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "later"], seed)
    _git(["push", "origin", "HEAD:main"], seed)
    return _git(["rev-parse", "HEAD"], seed).stdout.strip()


def test_detached_retry_uses_production_git_runner(tmp_path):
    from hermes_cli.update_cmd import _git_run as production_git_run

    _seed, checkout, first = _repo(tmp_path)
    target = update_revision.prepare_revision_target(production_git_run, ["git"], checkout, first)
    update_revision.checkout_revision(production_git_run, ["git"], checkout, target)
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], checkout).stdout.strip() == "HEAD"
    assert update_revision.prepare_revision_target(production_git_run, ["git"], checkout, first) == target
    with pytest.raises(RuntimeError, match="Could not fetch"):
        update_revision.prepare_revision_target(production_git_run, ["git"], checkout, "a" * 40)


def test_prepared_sha_remains_selected_when_remote_main_advances(tmp_path):
    seed, checkout, first = _repo(tmp_path)
    target = update_revision.prepare_revision_target(_git_run, ["git"], checkout, first)
    later = _advance(seed)

    rollback = update_revision.retain_precheckout_rollback(_git_run, ["git"], checkout, target)
    update_revision.checkout_revision(_git_run, ["git"], checkout, target)

    assert _git(["rev-parse", "HEAD"], checkout).stdout.strip() == first
    assert later != first
    assert _git(["rev-parse", rollback.ref], checkout).stdout.strip() == rollback.source_sha
    assert rollback.manifests["pyproject.toml"]


@pytest.mark.parametrize("revision", ["ABCDEF" * 6 + "ABCD", "a" * 39, "main"])
def test_malformed_revision_is_rejected_before_fetch_or_source_mutation(tmp_path, revision):
    _seed, checkout, first = _repo(tmp_path)
    with pytest.raises(ValueError):
        update_revision.prepare_revision_target(_git_run, ["git"], checkout, revision)
    assert _git(["rev-parse", "HEAD"], checkout).stdout.strip() == first


def test_unavailable_or_dirty_target_is_rejected_before_checkout(tmp_path):
    _seed, checkout, first = _repo(tmp_path)
    missing = "a" * 40
    with pytest.raises(RuntimeError, match="Could not fetch"):
        update_revision.prepare_revision_target(_git_run, ["git"], checkout, missing)
    (checkout / "dirty.txt").write_text("dirty\n")
    with pytest.raises(RuntimeError, match="dirty"):
        update_revision.prepare_revision_target(_git_run, ["git"], checkout, first)
    assert _git(["rev-parse", "HEAD"], checkout).stdout.strip() == first


def test_divergent_branch_is_rejected_before_checkout(tmp_path):
    seed, checkout, first = _repo(tmp_path)
    _git(["config", "user.email", "test@example.com"], checkout)
    _git(["config", "user.name", "Test"], checkout)
    _advance(seed)
    (checkout / "local.txt").write_text("local\n")
    _git(["add", "."], checkout)
    _git(["commit", "-m", "local"], checkout)
    _git(["fetch", "origin", "main"], checkout)
    before = _git(["rev-parse", "HEAD"], checkout).stdout.strip()
    with pytest.raises(RuntimeError, match="divergent"):
        update_revision.prepare_revision_target(_git_run, ["git"], checkout, first)
    assert _git(["rev-parse", "HEAD"], checkout).stdout.strip() == before


def test_unexpected_head_is_refused_and_same_target_skips_apply(monkeypatch, tmp_path):
    seed, checkout, first = _repo(tmp_path)
    later = _advance(seed)
    target = update_revision.prepare_revision_target(_git_run, ["git"], checkout, first)
    _git(["fetch", "origin", "main"], checkout)
    _git(["checkout", "--detach", later], checkout)
    with pytest.raises(RuntimeError, match="not approved"):
        update_revision.verify_revision_head(_git_run, ["git"], checkout, first)

    import hermes_cli.update_cmd as update_cmd
    calls = []
    monkeypatch.setattr(update_cmd, "_capture_head_sha", lambda *_: first)
    monkeypatch.setattr(update_cmd, "_current_branch_name", lambda *_args, **_kw: "main")
    def finish(*args, **kwargs):
        calls.append("repair")
        kwargs["final_head_guard"]()
    monkeypatch.setattr(update_cmd, "_finish_already_up_to_date", finish)
    monkeypatch.setattr(update_revision, "verify_revision_head", lambda *args: calls.append("verify"))
    monkeypatch.setattr(update_cmd, "_apply_pulled_update", lambda *args, **kwargs: calls.append("apply"))
    monkeypatch.setattr(update_cmd, "_verify_pinned_runtime_readback", lambda sha: calls.append("runtime"))
    opts = SimpleNamespace(assume_yes=True, gw_input_fn=None, active_lazy_features=None, active_tool_dependencies=None)
    update_cmd._run_pinned_revision_update(
        ["git"], update_revision.RevisionTarget(first, "tree"), opts, None,
        gateway_mode=False, desktop_dir=tmp_path, had_desktop_app_before_update=False,
        pre_update_snapshot_id=None, _windows_gateway_resume=None,
    )
    assert calls == ["verify", "repair", "verify", "runtime"]


def test_already_pinned_update_rejects_checkout_movement_during_catchup(monkeypatch, tmp_path):
    import hermes_cli.update_cmd as update_cmd
    seed, checkout, first = _repo(tmp_path)
    later = _advance(seed)
    _git(["fetch", "origin", "main"], checkout)
    outcomes = []
    monkeypatch.setattr(update_cmd, "_git_run", _git_run)
    monkeypatch.setattr(update_cmd, "_current_branch_name", lambda *_args, **_kwargs: "main")
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        PROJECT_ROOT=checkout,
        _resume_windows_gateways_after_update=lambda *_args: None,
    ))
    monkeypatch.setattr(update_cmd, "_finalize_receipt", lambda status, *_args: outcomes.append(status))
    monkeypatch.setattr(update_cmd, "_verify_pinned_runtime_readback", lambda _sha: None)
    def catchup(*args, **kwargs):
        _git(["checkout", "--detach", later], checkout)
        kwargs["final_head_guard"]()
    monkeypatch.setattr(update_cmd, "_finish_already_up_to_date", catchup)
    opts = SimpleNamespace(assume_yes=True, gw_input_fn=None, active_lazy_features=None, active_tool_dependencies=None)
    with pytest.raises(SystemExit) as error:
        update_cmd._run_pinned_revision_update(
            ["git"], update_revision.RevisionTarget(first, "tree"), opts, None,
            gateway_mode=False, desktop_dir=tmp_path, had_desktop_app_before_update=False,
            pre_update_snapshot_id=None, _windows_gateway_resume=None,
        )
    assert error.value.code == 1
    assert outcomes == ["failed"]
    assert _git(["rev-parse", "HEAD"], checkout).stdout.strip() == later


def test_public_cmd_update_preserves_revision_flag_to_native_pipeline(monkeypatch):
    import hermes_cli.main as main
    import hermes_cli.update_cmd as update_cmd

    seen = {}
    monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
    monkeypatch.setattr(main, "_finalize_update_output", lambda *_: None)
    monkeypatch.setattr(main, "_finalize_update_receipt", lambda *args: None)

    class Lock:
        holder = None
        def acquire(self): return True
        def release(self): return None
    import hermes_cli.update_lock as update_lock
    monkeypatch.setattr(update_lock, "UpdateLock", Lock)
    monkeypatch.setattr(update_cmd, "_cmd_update_impl", lambda args, gateway_mode: seen.update(
        revision=args.revision, gateway_mode=gateway_mode))
    args = SimpleNamespace(revision="a" * 40, gateway=False)
    main.cmd_update(args)
    assert seen == {"revision": "a" * 40, "gateway_mode": False}


def test_unknown_live_runtime_sha_is_not_verified_under_explicit_pin(monkeypatch):
    import hermes_cli.update_receipt as receipt
    import hermes_cli.update_cmd as update_cmd
    monkeypatch.setattr(receipt, "collect_fleet_versions", lambda **_kw: [{"state": "running", "code_sha": None}])
    with pytest.raises(RuntimeError, match="unknown"):
        update_cmd._verify_pinned_runtime_readback("a" * 40)


def test_invalid_target_python_is_rejected_before_checkout(tmp_path):
    seed, checkout, first = _repo(tmp_path)
    (seed / 'run_agent.py').write_text('def broken(:\n')
    bad = _advance(seed)
    with pytest.raises(RuntimeError, match='cannot compile'):
        update_revision.prepare_revision_target(_git_run, ['git'], checkout, bad)
    assert _git(['rev-parse', 'HEAD'], checkout).stdout.strip() == first
    assert not _git(['for-each-ref', 'refs/hermes-update-backups'], checkout).stdout.strip()


def test_pinned_probe_failure_is_unknown_not_an_empty_success(monkeypatch):
    from hermes_cli import update_receipt, update_cmd
    def unavailable():
        raise OSError('fixture probe unavailable')
    monkeypatch.setattr(update_receipt, '_profile_homes', unavailable)
    assert update_receipt.collect_fleet_versions() == []
    with pytest.raises(RuntimeError, match='identity is unknown'):
        update_cmd._verify_pinned_runtime_readback('a' * 40)


def test_completion_requires_both_checkout_and_live_runtime_identity(tmp_path, monkeypatch):
    from hermes_cli import main, update_cmd, update_receipt
    seed, checkout, first = _repo(tmp_path)
    monkeypatch.setattr(main, 'PROJECT_ROOT', checkout)
    monkeypatch.setattr(update_receipt, 'collect_fleet_versions',
                        lambda **_kw: [{'state': 'unknown', 'code_sha': None}])
    with pytest.raises(RuntimeError, match='unknown'):
        update_cmd._verify_pinned_completion(['git'], first)
    monkeypatch.setattr(update_receipt, 'collect_fleet_versions',
                        lambda **_kw: [{'state': 'current', 'code_sha': first}])
    assert update_cmd._verify_pinned_completion(['git'], first) is None
    later = _advance(seed)
    _git(['fetch', 'origin', later], checkout)
    _git(['checkout', '--detach', later], checkout)
    with pytest.raises(RuntimeError, match='not approved'):
        update_cmd._verify_pinned_completion(['git'], first)


def test_public_update_parser_rejects_conflicting_revision_selectors():
    import argparse
    from hermes_cli.subcommands.update import build_update_parser
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(dest='command'), cmd_update=lambda args: None)
    sha = 'a' * 40
    assert parser.parse_args(['update', '--revision', sha]).revision == sha
    with pytest.raises(SystemExit) as error:
        parser.parse_args(['update', '--branch', 'main', '--revision', sha])
    assert error.value.code == 2


def test_failed_runtime_readback_still_runs_catch_up(monkeypatch, tmp_path):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_revision as update_revision

    calls = []
    monkeypatch.setattr(update_cmd, "_capture_head_sha", lambda *_: "a" * 40)
    monkeypatch.setattr(update_cmd, "_current_branch_name", lambda *_args, **_kw: "main")
    def finish(*args, **kwargs):
        calls.append("repair")
        kwargs["final_head_guard"]()
    monkeypatch.setattr(update_cmd, "_finish_already_up_to_date", finish)
    monkeypatch.setattr(update_revision, "verify_revision_head", lambda *args: "a" * 40)
    monkeypatch.setattr(update_cmd, "_verify_pinned_runtime_readback", lambda sha: (_ for _ in ()).throw(RuntimeError("identity is unknown")))
    monkeypatch.setattr(update_cmd, "_finalize_receipt", lambda *args, **kwargs: calls.append("failed"))
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        PROJECT_ROOT=tmp_path,
        _resume_windows_gateways_after_update=lambda *a, **k: calls.append("resume"),
    ))
    monkeypatch.setattr(update_cmd.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    opts = SimpleNamespace(assume_yes=True, gw_input_fn=None, active_lazy_features=None, active_tool_dependencies=None)
    with pytest.raises(SystemExit) as error:
        update_cmd._run_pinned_revision_update(
            ["git"], update_revision.RevisionTarget("a" * 40, "tree"), opts, None,
            gateway_mode=False, desktop_dir=tmp_path, had_desktop_app_before_update=False,
            pre_update_snapshot_id=None, _windows_gateway_resume=None,
        )
    assert error.value.code == 1
    assert calls == ["repair", "resume", "failed"]


def test_pinned_prepare_discards_machine_dirt_before_dirty_check(monkeypatch, tmp_path):
    import hermes_cli.update_cmd as update_cmd

    calls = []
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(PROJECT_ROOT=tmp_path))
    monkeypatch.setattr(update_cmd, "_base_git_cmd", lambda: ["git"])
    monkeypatch.setattr(update_cmd, "_ensure_non_trampoline_git", lambda cmd: cmd)
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *args: calls.append("lockfile"))
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *args: calls.append("eol"))
    (tmp_path / ".git").mkdir()
    assert update_cmd._prepare_git_command(pinned_revision=True) == (False, ["git"], False)
    assert calls == ["lockfile", "eol"]


def test_finish_already_up_to_date_verifies_runtime_after_catchup(monkeypatch, tmp_path):
    import hermes_cli.update_cmd as update_cmd

    calls = []
    plan = update_cmd._CheckoutPlan(
        auto_stash_ref=None, commit_count=0, in_place_update=False,
        parked_branch_switched=False, prompt_for_restore=False,
        switch_block_reason=None, upstream_checked=True,
    )
    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(update_cmd, "_repair_current_checkout", lambda **kwargs: calls.append("repair") or True)
    monkeypatch.setattr(update_cmd, "_apply_pending_fleet_restart_catchup", lambda: calls.append("catchup"))
    monkeypatch.setattr(update_cmd, "_print_verified_update_completion", lambda message: calls.append(message) or True)
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        PROJECT_ROOT=tmp_path,
        _restore_stashed_changes=lambda *a, **k: None,
        _resume_windows_gateways_after_update=lambda *a, **k: None,
    ))
    update_cmd._finish_already_up_to_date(
        ["git"], "pinned revision", "HEAD", plan, assume_yes=True, gateway_mode=False,
        gw_input_fn=None, pre_update_snapshot_id=None, desktop_dir=tmp_path,
        had_desktop_app_before_update=False, active_lazy_features=None,
        active_tool_dependencies=None, _windows_gateway_resume=None,
        final_head_guard=lambda: calls.append("runtime"),
    )
    assert calls == ["repair", "catchup", "runtime", "✓ Already up to date!"]


def test_deferred_verified_completion_false_exits_partial(monkeypatch, tmp_path):
    import hermes_cli.update_cmd as update_cmd

    plan = update_cmd._CheckoutPlan(
        auto_stash_ref=None, commit_count=0, in_place_update=False,
        parked_branch_switched=False, prompt_for_restore=False,
        switch_block_reason=None, upstream_checked=True,
    )
    calls = []
    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(update_cmd, "_repair_current_checkout", lambda **kwargs: True)
    monkeypatch.setattr(update_cmd, "_apply_pending_fleet_restart_catchup", lambda: None)
    monkeypatch.setattr(update_cmd, "_print_verified_update_completion", lambda message: False)
    monkeypatch.setattr(update_cmd, "_write_gateway_update_exit_code", lambda ok: calls.append(("exit", ok)))
    monkeypatch.setattr(update_cmd, "_finalize_receipt", lambda *args, **kwargs: calls.append(args[0]))
    monkeypatch.setattr(update_cmd.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        PROJECT_ROOT=tmp_path,
        _restore_stashed_changes=lambda *a, **k: None,
        _resume_windows_gateways_after_update=lambda *a, **k: None,
    ))
    with pytest.raises(SystemExit) as error:
        update_cmd._finish_already_up_to_date(
            ["git"], "pinned revision", "HEAD", plan, assume_yes=True, gateway_mode=True,
            gw_input_fn=None, pre_update_snapshot_id=None, desktop_dir=tmp_path,
            had_desktop_app_before_update=False, active_lazy_features=None,
            active_tool_dependencies=None, _windows_gateway_resume=None,
            final_head_guard=lambda: calls.append("runtime"),
        )
    assert error.value.code == 1
    assert calls == ["runtime", ("exit", False), "partial"]


def test_pinned_checkout_failure_finalizes_failed_receipt(monkeypatch, tmp_path):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_revision as update_revision

    calls = []
    monkeypatch.setattr(update_cmd, "_capture_head_sha", lambda *_: "b" * 40)
    monkeypatch.setattr(update_cmd, "_current_branch_name", lambda *_args, **_kw: "main")
    monkeypatch.setattr(update_revision, "retain_precheckout_rollback", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("checkout failed")))
    monkeypatch.setattr(update_cmd, "_apply_pulled_update", lambda *a, **k: calls.append("apply"))
    monkeypatch.setattr(update_cmd, "_write_gateway_update_exit_code", lambda ok: calls.append(("exit", ok)))
    monkeypatch.setattr(update_cmd, "_finalize_receipt", lambda *args, **kwargs: calls.append(args[0]))
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        PROJECT_ROOT=tmp_path,
        _resume_windows_gateways_after_update=lambda *a, **k: calls.append("resume"),
    ))
    monkeypatch.setattr(update_cmd.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    opts = SimpleNamespace(assume_yes=True, gw_input_fn=None, active_lazy_features=None, active_tool_dependencies=None)
    with pytest.raises(SystemExit) as error:
        update_cmd._run_pinned_revision_update(
            ["git"], update_revision.RevisionTarget("a" * 40, "tree"), opts, None,
            gateway_mode=True, desktop_dir=tmp_path, had_desktop_app_before_update=False,
            pre_update_snapshot_id=None, _windows_gateway_resume="resume-token",
        )
    assert error.value.code == 1
    assert calls == [("exit", False), "resume", "failed"]
    assert "apply" not in calls
