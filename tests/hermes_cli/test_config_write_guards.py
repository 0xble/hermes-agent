"""Runtime routes are never the authored document used for unrelated writes."""
from copy import deepcopy

import hermes_yaml as yaml
import pytest

from hermes_cli import config
from hermes_cli.model_presets import ModelPresetError


@pytest.fixture
def authored(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    value = {
        "_config_version": 50,
        "model_presets": {"p": {"provider": "openrouter", "model": "primary", "reasoning_effort": "high",
                                  "fallbacks": [{"provider": "openrouter", "model": "backup"}]}},
        "delegation": {"model_preset": "p", "max_concurrent": 7},
        "terminal": {"cwd": "${GUARD_TEST_CWD}"},
    }
    monkeypatch.setenv("GUARD_TEST_CWD", "/resolved/runtime/path")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(value), encoding="utf-8")
    return value


@pytest.mark.parametrize("merge_existing", [False, True])
def test_runtime_writer_round_trips_raw_document(authored, merge_existing):
    loaded = config.load_config()
    assert loaded["delegation"]["model"] == "primary"
    assert loaded["terminal"]["cwd"] == "/resolved/runtime/path"
    loaded.setdefault("platforms", {}).setdefault("slack", {})["tool_progress"] = False
    config.save_config(loaded, merge_existing=merge_existing)
    expected = deepcopy(authored)
    expected["platforms"] = {"slack": {"tool_progress": False}}
    assert config.read_raw_config() == expected
    assert config.load_config()["delegation"]["model"] == "primary"


def test_validation_resolves_env_only_in_a_disposable_copy(authored, monkeypatch):
    monkeypatch.setenv("GUARD_TEST_ZONE", "America/Los_Angeles")
    authored["timezone"] = "${GUARD_TEST_ZONE}"
    config.save_config(authored)
    assert config.read_raw_config()["timezone"] == "${GUARD_TEST_ZONE}"
    assert config.load_config()["timezone"] == "America/Los_Angeles"


def test_platform_writer_reads_only_the_raw_document(authored, monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: pytest.fail("writer loaded a resolved config"))
    config.write_platform_config_field("slack", "tool_progress", False)
    expected = deepcopy(authored)
    expected["platforms"] = {"slack": {"tool_progress": False}}
    assert config.read_raw_config() == expected


@pytest.mark.parametrize("key", ["fallback_providers", "model", "provider", "reasoning_effort"])
def test_config_set_force_cannot_mix_route_fields_with_a_preset(authored, key, capsys):
    path = config.get_config_path()
    before = path.read_bytes()
    with pytest.raises(SystemExit) as exc:
        config.set_config_value(f"delegation.{key}", '[{"provider":"openrouter","model":"b"}]' if key == "fallback_providers" else "inline", force=True)
    assert exc.value.code != 0
    assert "cannot be combined" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_migration_refuses_invalid_preset_without_stamping(authored):
    path = config.get_config_path()
    authored["delegation"]["provider"] = "openrouter"
    path.write_text(yaml.safe_dump(authored), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ModelPresetError, match="cannot be combined"):
        config.migrate_config(interactive=False, quiet=True)
    assert path.read_bytes() == before


def test_migration_refuses_invalid_result_of_a_schema_step(authored, monkeypatch):
    path = config.get_config_path()
    before = path.read_bytes()
    def break_route(*args, **kwargs):
        candidate = config.read_raw_config()
        candidate["delegation"]["model"] = "inline"
        config._persist_migration(candidate)
    from hermes_cli import config_migrations
    monkeypatch.setattr(config_migrations, "run_migrations", break_route)
    with pytest.raises(ModelPresetError, match="cannot be combined"):
        config.migrate_config(interactive=False, quiet=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("writer", [config.save_config, lambda value: config.atomic_config_write(config.get_config_path(), value), lambda value: config.atomic_config_replace(config.get_config_path(), value)])
def test_save_refuses_an_invalid_preset_result(authored, writer):
    path = config.get_config_path()
    before = path.read_bytes()
    authored["delegation"]["model"] = "inline"
    with pytest.raises(ModelPresetError, match="cannot be combined"):
        writer(authored)
    assert path.read_bytes() == before
