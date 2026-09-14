"""Invariants for ``hermes_cli.config_effective.load_user_config_effective`` — the one loader every
defaults-free config reader (gateway runtime, TUI gateway, cron, ``hermes send`` bridge, doctor,
bootstrap modules) goes through."""
import textwrap

import pytest
import yaml


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("FIXTURE_USER_KEY", "user-secret")
    monkeypatch.setenv("FIXTURE_MANAGED_URL", "https://managed.example")
    _reset_caches()
    return home, managed


def _reset_caches():
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    _reset_caches()


USER_YAML = """
    model:
      name: user/model
      api_key: ${FIXTURE_USER_KEY}
    provider: custom
    display:
      skin: user-skin
    """
MANAGED_YAML = """
    model:
      base_url: ${FIXTURE_MANAGED_URL}
    display:
      skin: managed-skin
    """


def test_effective_is_user_plus_managed_plus_env_with_no_defaults(homes):
    """Contract as a fixture: given user config.yaml X, managed overlay Y and env Z, the effective
    dict is exactly this literal — ``${VAR}`` expanded on both layers, managed keys winning,
    root ``provider`` migrated under ``model``, and no DEFAULT_CONFIG key introduced (a missing
    key stays missing). Per-message gateway reads (and the system prompt built from them) are
    pinned by this shape, not by re-running the implementation's primitives."""
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    _write(home / "config.yaml", USER_YAML)
    _write(managed / "config.yaml", MANAGED_YAML)

    effective = load_user_config_effective(home / "config.yaml")

    assert effective == {
        "model": {
            "default": "user/model",
            "provider": "custom",
            "api_key": "user-secret",
            "base_url": "https://managed.example",
        },
        "display": {"skin": "managed-skin"},
    }
    assert "agent" in DEFAULT_CONFIG  # would be present if defaults had been merged


def test_broken_yaml_serves_last_good_and_fail_closed_raises(homes):
    """A torn mid-edit write must not silently drop user overrides: the fail-open path serves the last
    successfully parsed user file through the same pipeline; ``fail_closed`` surfaces the error to
    callers that keep their own last-good state."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    _write(home / "config.yaml", USER_YAML)
    good = load_user_config_effective(home / "config.yaml")

    (home / "config.yaml").write_text("model: [unterminated", encoding="utf-8")
    _reset_caches_keep_last_good()

    assert load_user_config_effective(home / "config.yaml") == good
    with pytest.raises(yaml.YAMLError):  # the type _refresh_fallback_model's own last-good path keys on
        load_user_config_effective(home / "config.yaml", fail_closed=True)


def test_good_backup_is_written_only_for_the_active_home(homes, tmp_path):
    """Reading ANOTHER profile's config (doctor, TUI cwd lookup) is a read: it must not create
    ``backups/config/`` inside that profile. The active home keeps the last-good copy."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    other = tmp_path / "other-profile"
    other.mkdir()
    _write(home / "config.yaml", USER_YAML)
    _write(other / "config.yaml", USER_YAML)

    load_user_config_effective(other / "config.yaml")
    load_user_config_effective(home / "config.yaml")

    assert not (other / "backups").exists()
    assert list((home / "backups" / "config").glob("config.yaml.good.*"))


def _reset_caches_keep_last_good():
    import hermes_cli.config as cfg
    from hermes_cli import config_effective

    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()


def test_effective_expands_presets_after_managed_merge_without_defaults(homes):
    from hermes_cli import config as cfg
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    user = {
        "model_presets": {"fast": {"provider": "custom", "model": "user/model"}},
        "model": {"model_preset": "fast"},
    }
    path = home / "config.yaml"
    _write(path, yaml.safe_dump(user))
    _write(managed / "config.yaml", yaml.safe_dump({
        "model_presets": {"fast": {"model": "managed/model"}},
    }))
    effective = load_user_config_effective(path)
    assert effective["model"] == {"provider": "custom", "default": "managed/model"}
    assert "agent" not in effective
    assert "terminal" not in effective
    assert cfg.load_config()["model"]["default"] == effective["model"]["default"]
    assert yaml.safe_load(path.read_text()) == user


def test_fresh_process_backup_preserves_preset_and_flat_moa_semantics(homes):
    from hermes_cli import config as cfg

    home, managed = homes
    raw = {
        "model_presets": {"fast": {"provider": "custom", "model": "user/model"}},
        "model": {"model_preset": "fast"},
        "moa": {
            "reference_models": [{"provider": "custom", "model": "reference/model"}],
            "aggregator": {"provider": "custom", "model": "aggregate/model"},
        },
    }
    path = home / "config.yaml"
    _write(path, yaml.safe_dump(raw))
    _write(managed / "config.yaml", yaml.safe_dump({
        "model_presets": {"fast": {"model": "managed/model"}},
    }))
    good = cfg.load_config()
    assert good["model"]["default"] == "managed/model"
    assert "presets" not in good["moa"]
    path.write_text("model: [unterminated", encoding="utf-8")
    _reset_caches()
    cfg._LAST_EXPANDED_CONFIG_BY_PATH.clear()  # fresh-process recovery must use the disk copy
    recovered = cfg.load_config()
    assert recovered["model"] == good["model"]
    assert recovered["moa"] == good["moa"]
    assert path.read_text() == "model: [unterminated"


def test_gateway_loader_expands_each_authored_preset_layer(homes):
    from gateway.config_loader import read_yaml_layers

    home, managed = homes
    _write(home / "config.yaml", yaml.safe_dump({
        "model": {"provider": "custom", "default": "user/model"},
    }))
    _write(managed / "config.yaml", yaml.safe_dump({
        "model_presets": {"managed": {"provider": "custom", "model": "managed/model"}},
        "model": {"model_preset": "managed"},
    }))
    result = read_yaml_layers(home)
    assert result["model"] == {"provider": "custom", "default": "managed/model"}
    assert "terminal" not in result
