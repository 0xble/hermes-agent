"""Distinguishing outcomes of immutable release reconciliation."""

import pytest

from hermes_cli import update_cmd


@pytest.mark.parametrize("changes,expected", [
    ({"pending_transaction": True}, "complete-transaction"),
    ({"enabled": False, "current": "absent", "journal": "none"}, "no-op"),
    ({"enabled": False, "current": "absent", "journal": "in-progress"}, "no-op"),
    ({"enabled": False, "current": "absent", "journal": "done", "defer": True}, "no-op"),
    ({"enabled": False, "current": "absent", "journal": "rolled-back"}, "no-op"),
    ({"current": "equal", "candidate": "none"}, "fail-with-message"),
    ({"current": "equal", "candidate": "failed-partial"}, "fail-with-message"),
    ({"current": "equal", "candidate": "staged", "service": "current", "running": "current"}, "no-op"),
    ({"current": "equal", "candidate": "staged", "service": "none", "running": "none"}, "no-op"),
    ({"current": "equal", "candidate": "staged", "service": "source", "defer": True}, "defer-record"),
    ({"current": "equal", "candidate": "staged", "running": "other"}, "repair-service"),
    ({"current": "absent", "defer": True}, "defer-record"),
    ({"current": "different", "journal": "done", "defer": True}, "defer-record"),
    ({"candidate": "staged"}, "activate-staged"),
    ({"candidate": "failed-partial"}, "build+activate"),
    ({"current": "different", "journal": "done"}, "build+activate"),
    ({"current": "absent", "service": "current"}, ValueError),
    ({"current": "different", "journal": "none"}, ValueError),
])
def test_reconcile_matrix(changes, expected):
    values = dict(enabled=True, current="absent", candidate="none", journal="none",
                  service="none", running="none", defer=False)
    values.update(changes)
    state = update_cmd._ReleaseReconcileState(**values)
    if expected is ValueError:
        with pytest.raises(ValueError, match="unreachable"):
            update_cmd._reconcile_immutable_release(state)
    else:
        assert update_cmd._reconcile_immutable_release(state) == expected



def test_first_migration_failure_retries_without_ready_candidate(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases as releases
    home = tmp_path / "profile"
    home.mkdir()
    (home / "release-layout.json").write_text('{"source": "source"}')
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(update_cmd._m(), "_build_web_ui", lambda _: True)
    calls = []
    monkeypatch.setattr(update_cmd, "_activate_immutable_release", lambda **kw: calls.append(kw) or True)
    update_cmd._catch_up_immutable_release(defer=False)
    assert calls == [{"sha": "B", "source": update_cmd._m().PROJECT_ROOT}]


def test_opted_in_already_current_checkout_stages_first_migration(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases as releases
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(update_cmd._m(), "_build_web_ui", lambda _: True)
    calls = []
    monkeypatch.setattr(update_cmd, "_activate_immutable_release", lambda **kw: calls.append(kw) or True)
    update_cmd._catch_up_immutable_release(defer=False)
    assert calls == [{"sha": "B", "source": update_cmd._m().PROJECT_ROOT}]


def test_deferred_first_migration_stages_but_does_not_activate(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases as releases
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(update_cmd._m(), "_build_web_ui", lambda _: True)
    calls = []
    monkeypatch.setattr(update_cmd, "_activate_immutable_release", lambda **kw: calls.append(kw) or True)
    update_cmd._catch_up_immutable_release(defer=True)
    assert calls == [{"defer": True, "sha": "B", "source": update_cmd._m().PROJECT_ROOT}]
    assert not (home / "current").exists()


@pytest.mark.platforms("macos")
def test_equal_pointer_with_stale_service_repairs_on_noop(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases as releases, gateway, gateway_launchd
    home = tmp_path / "profile"
    candidate = home / "releases" / "B"
    candidate.mkdir(parents=True)
    (candidate / ".release-ready").write_text("B\n")
    (candidate / ".hermes_build_sha").write_text("B\n")
    releases.promote(home, candidate)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(update_cmd.sys, "platform", "darwin")
    plist = tmp_path / "service.plist"
    plist.write_text("source")
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda **kwargs: plist.read_text() == "release")
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda release_target=None: "release")
    reloads = []
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist",
                        lambda path: reloads.append(path) or True)
    with pytest.raises(SystemExit, match="1"):
        update_cmd._catch_up_immutable_release(defer=False)
    assert plist.read_text() == "release"
    assert reloads == [plist]
    assert (home / "release-txn.json").exists()


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("entrypoint", ["repair", "replay", "rollback-replay"])
@pytest.mark.parametrize("leftover", ["journal", "label"])
def test_leftovers_block_service_repair_and_transaction_replay(tmp_path, monkeypatch,
                                                              entrypoint, leftover):
    import json
    from types import SimpleNamespace
    from hermes_cli import immutable_releases as releases, forward_only_guard, gateway, gateway_launchd

    home = tmp_path / "profile"
    candidate = home / "releases" / "B"
    candidate.mkdir(parents=True)
    (candidate / ".release-ready").write_text("B\n")
    (candidate / ".hermes_build_sha").write_text("B\n")
    releases.promote(home, candidate)
    plist = tmp_path / "service.plist"
    plist.write_text("source")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"release_acknowledgement_timeout_seconds": 0})
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda **kwargs: plist.read_text() == "release")
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda release_target=None: "release")
    monkeypatch.setattr(releases, "acknowledge_running_release", lambda *a, **kw: False)
    if entrypoint != "repair":
        releases.activate_release(home, candidate, plist_path=plist, plist_body=b"release",
                                  reload_callback=lambda: "deferred", force_reload=True)
        # Model a crash before the write-ahead reload submission marker.
        pending = home / "release-txn.json"
        record = json.loads(pending.read_text())
        record.pop("reload_issued")
        if entrypoint == "rollback-replay":
            record["operation"] = "rollback"
        pending.write_text(json.dumps(record))
        plist.write_text("source")
    label = "ai.hermes.gateway.g-" + "a" * 32
    monkeypatch.setattr(forward_only_guard, "_loaded_forward_labels",
                        lambda **kw: [label] if leftover == "label" else [])
    monkeypatch.setattr(forward_only_guard, "_label_hermes_homes", lambda *a, **kw: [home])
    if leftover == "journal":
        (home / "forward-update.json").write_text('{"state": "draining"}')
    before = {path: path.read_bytes() for path in home.glob("*.json")}
    reloads = []
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist",
                        lambda path: reloads.append(path) or "deferred")
    with pytest.raises(RuntimeError, match="withdrawn forward-only state remains"):
        if entrypoint == "rollback-replay":
            update_cmd._cmd_update_impl(SimpleNamespace(rollback=True), gateway_mode=False)
        elif entrypoint == "replay":
            update_cmd._finish_pending_release_transaction(home)
        else:
            update_cmd._catch_up_immutable_release(defer=False)
    assert reloads == []
    assert plist.read_text() == "source"
    assert {path: path.read_bytes() for path in home.glob("*.json")} == before
    assert (home / "current").resolve() == candidate


def test_equal_pointer_with_stale_runtime_arms_fleet_catchup(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases as releases, update_receipt
    home = tmp_path / "profile"
    candidate = home / "releases" / "B"
    candidate.mkdir(parents=True)
    (candidate / ".release-ready").write_text("B\n")
    (candidate / ".hermes_build_sha").write_text("B\n")
    releases.promote(home, candidate)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **kw: [
        {"code_root": str(candidate), "code_sha": "A"}])
    calls = []
    monkeypatch.setattr(update_cmd, "_write_fleet_restart_pending_marker", lambda **kw: calls.append(kw))
    update_cmd._catch_up_immutable_release(defer=False)
    assert calls == [{"expected_sha": "B"}]
