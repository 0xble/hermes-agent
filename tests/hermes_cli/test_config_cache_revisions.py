"""All producers of the shared raw cache preserve stale-write protection."""
import pytest

from hermes_cli import config, config_effective
from hermes_cli.config_write_guards import ConfigWriteConflict


@pytest.mark.parametrize("readonly", [False, True])
def test_effective_reader_cannot_seed_an_unversioned_raw_cache(tmp_path, monkeypatch, readonly):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n", encoding="utf-8")
    config_effective.load_user_config_effective(fail_closed=True)
    reader = config.read_raw_config_readonly if readonly else config.read_raw_config
    stale = reader()
    if readonly:
        assert reader() is stale
    path.write_text("timezone: Pacific/Honolulu\n", encoding="utf-8")
    latest = path.read_bytes()
    with pytest.raises(ConfigWriteConflict):
        config.save_config(stale)
    assert path.read_bytes() == latest
