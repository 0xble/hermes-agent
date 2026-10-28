"""Snapshot metadata follows real refreshes, never YAML or independent stale copies."""
import copy
from io import StringIO

import pytest

import hermes_yaml as yaml
from hermes_cli import config
from hermes_cli.config_write_guards import ConfigWriteConflict, replace_snapshot


@pytest.mark.parametrize("refresh", ["reload", "consent"])
def test_in_place_refresh_advances_only_the_participating_snapshot(tmp_path, monkeypatch, refresh):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n", encoding="utf-8")
    participating = config.load_config()
    stale = copy.deepcopy(participating)
    if refresh == "reload":
        config.set_config_value("timezone", "Pacific/Honolulu")
        replace_snapshot(participating, config.load_config())
    else:
        from hermes_cli.observability.shared_metrics_consent import save_consent
        save_consent(False, False, participating)
    participating["display"]["skin"] = "mono"
    config.save_config(participating)
    before = path.read_bytes()
    with pytest.raises(ConfigWriteConflict):
        config.save_config(stale)
    assert path.read_bytes() == before
    loaded = config.load_config()
    assert loaded["display"]["skin"] == "mono"
    if refresh == "reload":
        assert loaded["timezone"] == "Pacific/Honolulu"
    else:
        assert config.read_raw_config()["telemetry"]["shared_metrics"]["enabled"] is False


def test_scoped_effective_read_never_retains_an_invalid_document(tmp_path, monkeypatch):
    from hermes_cli import config_effective as effective
    from hermes_cli.config_errors import ConfigValidationError

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("updates:\n  immutable_releases: invalid\n  release_acknowledgement_timeout_seconds: 18\n")
    with pytest.raises(ConfigValidationError):
        effective.load_user_config_effective(path, fail_closed=True)
    scoped = effective.load_user_config_effective(path, fail_closed=True, validate_structure=False)
    assert scoped["updates"]["release_acknowledgement_timeout_seconds"] == 18
    assert str(path) not in effective._LAST_GOOD_USER_RAW
    assert str(path) not in effective._EFFECTIVE_CACHE
    with pytest.raises(ConfigValidationError):
        effective.load_user_config_effective(path, fail_closed=True)


@pytest.mark.parametrize("text", ["updates: [unclosed\n", "- not-a-mapping\n"])
def test_scoped_effective_read_still_rejects_unreadable_documents(tmp_path, monkeypatch, text):
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.config_errors import ConfigValidationError

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises((yaml.YAMLError, ConfigValidationError)):
        load_user_config_effective(path, fail_closed=True, validate_structure=False)


def test_yaml_serializes_nested_snapshots_as_plain_mapping_data(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n", encoding="utf-8")
    snapshot = config.read_raw_config()
    payload = {"snapshot": snapshot, "copies": [snapshot.copy(), copy.deepcopy(snapshot)]}
    expected = {"snapshot": {"timezone": "UTC"}, "copies": [{"timezone": "UTC"}] * 2}
    stream = StringIO()
    yaml.safe_dump(payload, stream)
    assert yaml.safe_load(stream.getvalue()) == expected
    assert yaml.safe_load(yaml.safe_dump(payload)) == expected
    assert "source_revision" not in stream.getvalue()
    assert "ConfigSnapshot" not in stream.getvalue()
