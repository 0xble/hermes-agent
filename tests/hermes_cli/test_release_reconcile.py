"""No-op update reconciliation is exhaustive over the release transition axes."""
from itertools import product

import pytest

from hermes_cli import update_cmd


@pytest.mark.parametrize(
    "enabled,current,candidate,journal,service,running,defer",
    tuple(product(
        (False, True), ("absent", "equal", "different"),
        ("none", "staged", "failed-partial"),
        ("none", "in-progress", "done", "rolled-back"),
        ("none", "source", "current", "stale-release"),
        ("none", "source", "current", "other"), (False, True),
    )),
)
def test_reconcile_matrix(enabled, current, candidate, journal, service, running, defer):
    state = update_cmd._ReleaseReconcileState(enabled, current, candidate, journal, service, running, defer)
    # Physical constraints: an absent current cannot have a current service
    # or process; a completed migration has a release pointer.
    unreachable = ((current == "absent" and (service == "current" or running == "current"))
                   or (current != "absent" and journal in {"in-progress", "rolled-back"})
                   or (current == "absent" and journal == "done")
                   or (current == "different" and journal == "none"))
    if unreachable:
        with pytest.raises(ValueError, match="unreachable"):
            update_cmd._reconcile_immutable_release(state)
        return
    if current == "absent" and not enabled and journal in {"none", "rolled-back"}:
        expected = "no-op"
    elif current == "equal" and candidate != "staged":
        expected = "fail-with-message"
    elif current == "equal":
        expected = ("defer-record" if defer and (service not in {"none", "current"} or running not in {"none", "current"})
                    else "repair-service" if service not in {"none", "current"} or running not in {"none", "current"}
                    else "no-op")
    elif defer:
        expected = "defer-record"
    elif candidate == "staged":
        expected = "activate-staged"
    else:
        expected = "build+activate"
    assert update_cmd._reconcile_immutable_release(state) == expected, state


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


@pytest.mark.macos_only
def test_equal_pointer_with_stale_service_repairs_on_noop(tmp_path, monkeypatch):
    from hermes_cli import immutable_releases as releases, gateway
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
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda: plist.read_text() == "release")
    monkeypatch.setattr(gateway, "refresh_launchd_plist_if_needed", lambda: plist.write_text("release") or True)
    update_cmd._catch_up_immutable_release(defer=False)
    assert plist.read_text() == "release"


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
