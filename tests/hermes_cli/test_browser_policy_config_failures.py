"""Security-sensitive policy readers must distinguish omission from read failure."""
import pytest

from hermes_cli import browser_connect, config


@pytest.mark.parametrize("contents", ["browser: [broken", "- invalid-root", "[]", "false", "browser: []"])
def test_browser_policy_rejects_actual_invalid_config(tmp_path, monkeypatch, contents):
    path = tmp_path / "config.yaml"
    path.write_text(contents)
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    # Prime the tolerant cache too: it must not hide a malformed root later.
    config.read_raw_config()
    mode, error = browser_connect._real_profile_refresh_mode()
    assert mode is None and error is not None and "Could not read" in error


def test_browser_policy_rejects_actual_io_failure(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.mkdir()  # portable unreadable-as-a-file fixture, even for a privileged user
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    mode, error = browser_connect._real_profile_refresh_mode()
    assert mode is None and error is not None and "Could not read" in error


def test_browser_policy_successful_omission_and_initial_remain_supported(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    assert browser_connect._real_profile_refresh_mode() == ("launch", None)
    path.write_text("browser: {}\n")
    assert browser_connect._real_profile_refresh_mode() == ("launch", None)
    path.write_text("browser:\n  real_profile_refresh: initial\n")
    assert browser_connect._real_profile_refresh_mode() == ("initial", None)


def test_supported_refresh_mode_roundtrips_through_config_cli(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    config.set_config_value("browser.real_profile_refresh", "initial")
    output = capsys.readouterr()
    assert "not a recognized config key" not in (output.out + output.err).lower()
    assert browser_connect._real_profile_refresh_mode() == ("initial", None)


@pytest.mark.parametrize("damage", [None, "browser: [", "browser: []", "browser:\n  real_profile_refresh: invalid\n", "unreadable"])
def test_managed_refresh_policy_preserves_snapshot_auth(tmp_path, monkeypatch, damage):
    from pathlib import Path
    from tests.tools.test_browser_real_profile import TestSnapshotRealProfile, _auth_db

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    user = tmp_path / "config.yaml"
    user.write_text("browser:\n  real_profile_refresh: launch\n")
    monkeypatch.setattr(config, "get_config_path", lambda: user)
    managed = tmp_path / "managed"
    managed.mkdir()
    policy = managed / "config.yaml"
    policy.write_text("browser:\n  real_profile_refresh: initial\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    src = TestSnapshotRealProfile()._make_profile(tmp_path / "source")
    dst, error = browser_connect.snapshot_real_profile("chrome", src=str(src))
    assert error is None
    cookies = Path(dst) / "Default" / "Cookies"
    _auth_db(cookies, "managed-account")
    _auth_db(src / "Default" / "Cookies", "source-account")
    config.load_config_readonly()  # A warm tolerant cache cannot hide subsequent damage.
    if damage == "unreadable":
        policy.unlink()
        policy.mkdir()
    elif damage:
        policy.write_text(damage)
    result, error = browser_connect.snapshot_real_profile("chrome", src=str(src))
    if damage:
        assert result is None and error
    else:
        assert result == dst and error is None
    assert _auth_db(cookies) == "managed-account"
    if damage is None:
        policy.write_text("browser:\n  real_profile_refresh: launch\n")
        assert browser_connect.snapshot_real_profile("chrome", src=str(src)) == (dst, None)
        assert _auth_db(cookies) == "source-account"
