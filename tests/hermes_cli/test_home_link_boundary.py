"""Missing linked storage must never become a new local Hermes home."""

import pytest

from hermes_cli.config_home import HomeInitializationError, initialize_home


@pytest.mark.parametrize("at_home", [False, True])
def test_missing_link_target_is_not_materialized(tmp_path, monkeypatch, at_home):
    from hermes_cli import config

    target = tmp_path / "missing-volume" / "user"
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    home = link if at_home else link / "hermes"
    monkeypatch.setattr(config, "is_managed", lambda: False)
    ensured = set()
    with pytest.raises(HomeInitializationError):
        initialize_home(home, ("sessions", "logs"), ensured)
    assert link.is_symlink() and link.readlink() == target
    assert not target.exists() and not target.parent.exists()
    assert ensured == set()
