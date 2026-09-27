"""`hermes update --no-gateway-restart` (#93649).

A cron running inside the gateway's own cgroup cannot survive the fleet
restart phase (SIGUSR1 drain + systemd KillMode=mixed kills the updater
itself). The flag runs the full update pipeline but defers the restart;
the pending-restart marker is kept so a later normal update catches up.
"""
from types import SimpleNamespace
from unittest.mock import patch
import plistlib

import pytest

from hermes_cli import update_cmd as uc
from hermes_cli import update_cmd_fleet as fleet
from hermes_cli import immutable_releases as releases


def _opts(**overrides):
    base = dict(
        assume_yes=True, gw_input_fn=None, active_lazy_features=[],
        active_tool_dependencies=[], pre_update_version="1.0",
        discard_local_changes=False, keep_stash=False, switch_branch=False,
        no_gateway_restart=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)




def test_already_current_catchup_is_deferred_under_flag():
    """Already-up-to-date + pending marker + flag: no restart, no exit, marker kept."""
    with (
        patch.object(fleet, "_pending_fleet_restart_needed", return_value=True),
        patch.object(fleet, "_warn_pending_fleet_restart"),
        patch.object(uc, "_run_pending_fleet_restart") as mock_run,
        patch.object(fleet, "_clear_fleet_restart_pending_marker") as mock_clear,
    ):
        fleet._apply_pending_fleet_restart_catchup(defer=True)
    mock_run.assert_not_called()
    mock_clear.assert_not_called()


@pytest.mark.macos_only
def test_pending_release_no_restart_does_not_replay_or_mutate(tmp_path, monkeypatch):
    """A restart-prohibited catch-up cannot alter an unacknowledged transaction."""
    home = tmp_path / "profile"
    a, b = home / "releases/A", home / "releases/B"
    for root in (a, b):
        root.mkdir(parents=True)
        (root / ".release-ready").write_text(root.name + "\n")
        (root / ".hermes_build_sha").write_text(root.name + "\n")
        (root / "pyproject.toml").write_text("[project]\nname='probe'\n")
        (root / "uv.lock").write_text("")
        python = root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
    releases.promote(home, a)
    plist = tmp_path / "ai.hermes.s2-norestart.plist"
    original = plistlib.dumps({"Label": plist.stem, "EnvironmentVariables": {"HERMES_HOME": str(home)},
                               "ProgramArguments": [str(a / ".venv/bin/python")]})
    intended = plistlib.dumps({"Label": plist.stem, "EnvironmentVariables": {"HERMES_HOME": str(home)},
                               "ProgramArguments": [str(b / ".venv/bin/python")]})
    plist.write_bytes(original)
    result = releases.activate_release(home, b, plist_path=plist,
                                       plist_body=intended,
                                       reload_callback=lambda: "deferred")
    assert result["reload_pending"]
    txn = home / "release-txn.json"
    before = {p: p.read_bytes() for p in (txn, plist)}
    before_pointers = (releases.read_pointer(home / "current"),
                       releases.read_pointer(home / "previous"))
    monkeypatch.setattr(uc, "get_hermes_home", lambda: home)
    monkeypatch.setattr(releases, "acknowledge_running_release", lambda _: False)
    with (
        patch.object(uc, "_finish_pending_release_transaction", side_effect=AssertionError("replayed")),
        patch.object(uc, "_activate_immutable_release", side_effect=AssertionError("activated")),
        patch.object(uc, "_finalize_receipt"),
        pytest.raises(SystemExit, match="restart-prohibited"),
    ):
        uc._catch_up_immutable_release(defer=True, sha="B", source=tmp_path)
    assert {p: p.read_bytes() for p in before} == before
    assert (releases.read_pointer(home / "current"),
            releases.read_pointer(home / "previous")) == before_pointers
