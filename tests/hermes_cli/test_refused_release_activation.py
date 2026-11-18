"""A rejected candidate must not become durable transaction intent."""
import pytest

from hermes_cli import immutable_releases as releases
from tests.hermes_cli.test_release_extras_preflight import package, release, selected


@pytest.mark.parametrize("followup", ["wal", "rollback", "healthy-promote"])
def test_refused_promote_leaves_recovery_and_future_updates_usable(tmp_path, monkeypatch, followup):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "frozen-source")
    z, a, b, c = [release(home, name, {"widget": "widget-client"}) for name in "ZABC"]
    for root in (z, a, c):
        package(selected(root, tmp_path, ["widget"]), "widget-client", "widget_client")
    releases.promote(home, z)
    releases.promote(home, a)

    with pytest.raises(RuntimeError, match="refused"):
        releases.promote(home, b)

    assert releases.read_pointer(home / "current") == a
    assert releases.read_pointer(home / "previous") == z
    if followup == "wal":
        assert not (home / "release-txn.json").exists()
    elif followup == "rollback":
        releases.rollback(home)
        assert releases.read_pointer(home / "current") == z
    else:
        releases.promote(home, c)
        assert releases.read_pointer(home / "current") == c
        assert releases.read_pointer(home / "previous") == a
