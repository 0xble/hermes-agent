"""Shared profile mutations during immutable staging must invalidate unchanged notices."""
import json
from types import SimpleNamespace

import pytest

from gateway.update_notifications import _immutable_unchanged_proof, final_outcome
from tests.gateway.test_update_failure_notice import notification_checkout  # fixture
from tests.gateway.update_fixtures import _release, pending
from tests.hermes_cli.test_update_target_identity import git, update_tree  # fixture


@pytest.fixture
def staged_update(update_tree, tmp_path, monkeypatch):
    from hermes_cli import main, update_receipt

    home = tmp_path / "home"
    home.mkdir()
    release = _release(home, sha=update_tree.base)
    source = update_tree.clone
    target = git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "candidate")
    git(source, "fetch", "--no-tags", ".", target)
    (home / "release-layout.json").write_text(json.dumps({"source": str(source)}))
    candidate = home / "releases" / target
    candidate.mkdir()
    for name in (".release-ready", ".hermes_build_sha"):
        (candidate / name).write_text(target)
    request = pending(home, reason=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(main, "PROJECT_ROOT", source)
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: home / "logs/update_receipts")
    monkeypatch.setattr(update_receipt, "_code_identity", lambda **_: {"sha": git(source, "rev-parse", "HEAD")})
    monkeypatch.setattr("gateway.update_notifications.__file__", str(release / "gateway/update_notifications.py"))
    return SimpleNamespace(home=home, release=release, candidate=candidate, request=request)


def fail_apply(fixture, message, *, unchanged=False):
    from hermes_cli import update_cmd, update_receipt

    with update_receipt.update_receipt_scope():
        update_receipt.begin_update_receipt()
        with pytest.raises(RuntimeError, match=message):
            update_cmd._apply_fetched_immutable_update(
                ["git"], "main", SimpleNamespace(no_gateway_restart=True), None,
                gateway_mode=True, had_desktop_app_before_update=False,
                pre_update_snapshot_id=None, _pre_update_plan=None, _windows_gateway_resume=None)
        path = update_receipt.finalize_update_receipt("failed", stop_reason=message)
    (fixture.home / ".update_process_exit_code").write_text("1")
    assert path is not None
    receipt = json.loads(path.read_text())
    success, detail = final_outcome(fixture.home, fixture.request)
    assert success is False
    if unchanged:
        assert "Nothing changed. Still on " in detail and "Runtime state is unverified" not in detail
    else:
        assert "Runtime state is unverified" in detail and "Nothing changed" not in detail
    assert _immutable_unchanged_proof(fixture.home, receipt) is unchanged
    return receipt


@pytest.mark.parametrize("failure", ["smoke", "identity", "sync"])
def test_eviction_published_during_staging_prevents_unchanged_notice(staged_update, monkeypatch, failure):
    from hermes_cli import immutable_releases as releases
    from pm.plugin_eviction import PluginEviction

    home, candidate = staged_update.home, staged_update.candidate
    profile = home / "profiles" / "other"
    profile.mkdir(parents=True)
    config = 'plugins:\n  enabled: [incompatible]\nmemory:\n  provider: incompatible\n'
    for directory in (home, profile):
        (directory / "config.yaml").write_text(config)
    monkeypatch.setattr("pm.plugins_state.dependency_homes", lambda: [home, profile])
    monkeypatch.setattr("pm.paths.repo_root", lambda: candidate)
    monkeypatch.setattr(releases, "_leaf_requirements", lambda _: {"slack": {"slack-bolt"}})
    monkeypatch.setattr(releases, "_active_distributions", lambda _: {})
    monkeypatch.setattr(releases, "_committed_extras", lambda *_: {"slack"})

    def sync(*_a, **kw):
        assert kw["evict_incompatible_plugins"] is True
        entries = [(directory / "plugins", "incompatible", directory / "plugins/incompatible")
                   for directory in (home, profile)]
        PluginEviction(entries, {entry[2].resolve(): "requires a different Python" for entry in entries}).publish(candidate)
        if failure == "sync":
            raise RuntimeError("sync result lost after publication")

    def smoke(*_a, **_kw):
        if failure == "smoke":
            raise RuntimeError("candidate plugin smoke failed")
        (candidate / ".release-ready").write_text("invalid")

    monkeypatch.setattr("pm.sync_venv", sync)
    monkeypatch.setattr(releases, "smoke_plugins", smoke)
    message = {"smoke": "candidate plugin smoke failed", "identity": "staged release does not match fetched commit",
               "sync": "sync result lost after publication"}[failure]
    receipt = fail_apply(staged_update, message)
    assert receipt["immutable_update"]["activation_started"] is False
    assert receipt["immutable_update"]["before"] == receipt["immutable_update"]["after"]
    assert receipt["staging_runtime_mutation_started"] is True
    from hermes_yaml import roundtrip_yaml
    for directory in (home, profile):
        mutated = roundtrip_yaml().load((directory / "config.yaml").read_text())
        assert mutated["plugins"]["disabled"] == ["incompatible"]
        assert mutated["memory"]["provider"] == ""


def test_fresh_dependency_validation_failure_can_still_prove_unchanged(staged_update, monkeypatch):
    from hermes_cli import immutable_releases as releases

    for marker in staged_update.candidate.iterdir():
        marker.unlink()
    staged_update.candidate.rmdir()

    def fail(*_a, **_kw):
        raise RuntimeError("candidate dependency validation failed")

    stage_tree = releases._stage_git_tree

    def archive(source, staging, sha):
        stage_tree(source, staging, sha)
        (staging / "hermes_cli").mkdir()
        (staging / "hermes_cli/immutable_releases.py").touch()

    monkeypatch.setattr(releases, "_stage_git_tree", archive)
    monkeypatch.setattr(releases, "_build_venv", fail)
    receipt = fail_apply(staged_update, "candidate dependency validation failed", unchanged=True)
    assert receipt["staging_runtime_mutation_started"] is False
    assert receipt["immutable_update"]["activation_started"] is False


@pytest.mark.parametrize("pointer_present", [True, False])
def test_early_immutable_target_selection_failure_stays_unverified(staged_update, monkeypatch, pointer_present):
    if not pointer_present:
        (staged_update.home / "current").unlink()

    def fail(*_a, **_kw):
        raise RuntimeError("candidate selection failed")

    monkeypatch.setattr("hermes_cli.update_cmd_candidate.select_update_target", fail)
    receipt = fail_apply(staged_update, "candidate selection failed")
    assert "immutable_update" not in receipt
    assert any(step["name"] == "immutable_source_untouched" and step["ok"] is True for step in receipt["steps"])
