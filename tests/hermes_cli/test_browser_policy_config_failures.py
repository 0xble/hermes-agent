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
