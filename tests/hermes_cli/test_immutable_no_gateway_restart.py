"""Immutable promotion never restarts through a restart-prohibited invocation."""
import plistlib
from unittest.mock import patch
import pytest
from hermes_cli import update_cmd as uc
from hermes_cli import immutable_releases as releases

@pytest.mark.platforms("macos")
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
    monkeypatch.setattr(releases, "acknowledge_running_release", lambda _, **_kw: False)
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
