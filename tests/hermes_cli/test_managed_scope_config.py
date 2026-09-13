"""Config integration tests — managed scope wins over user config at the leaf."""
import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


def test_managed_beats_user(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model:\n  default: managed/model\n")
    assert cfg_get(load_config(), "model", "default") == "managed/model"


def test_managed_list_wins_wholesale(homes):
    """D3: a managed list value replaces the user's wholesale."""
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "toolsets:\n  enabled: [a, b, c]\n")
    _write(managed / "config.yaml", "toolsets:\n  enabled: [x]\n")
    assert cfg_get(load_config(), "toolsets", "enabled") == ["x"]


def test_user_cannot_shadow_managed_literal_via_envref(homes, monkeypatch):
    """A managed literal must NOT be expandable via a ${VAR} the user controls.

    The managed value is a plain literal 'managed/locked' with no ${...}, so a
    user-defined env var has nothing to substitute. This asserts the managed
    literal survives verbatim regardless of user env, and that managed wins.
    """
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    monkeypatch.setenv("EVIL", "user/override")
    _write(home / "config.yaml", "model:\n  default: ${EVIL}\n")
    _write(managed / "config.yaml", "model:\n  default: managed/locked\n")
    assert cfg_get(load_config(), "model", "default") == "managed/locked"


def test_managed_nested_dict_default_flattens_on_load(homes):
    """A dict-valued managed ``model.default`` must flatten on load.

    ``load_config()`` merges the managed overlay after its single
    normalization pass, so a managed ``model.default: {provider: ...,
    model: ...}`` used to reach runtime readers as a raw dict. The overlay
    is now normalized before merging (parity with
    ``managed_scope.apply_managed_overlay``), so the merged config exposes a
    string ``default`` paired with the nested ``provider``.
    """
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model:\n  default:\n    provider: nous\n    model: managed/nested\n")
    cfg = load_config()
    assert cfg_get(cfg, "model", "default") == "managed/nested"
    assert cfg_get(cfg, "model", "provider") == "nous"


def test_managed_bare_string_model_flattens_to_default_on_load(homes):
    """A bare ``model: <string>`` in the managed file stays a dict shape.

    Mirrors the existing managed-overlay contract: a bare string model must
    merge as ``model.default`` so readers that do
    ``cfg["model"]["default"]`` keep working (never a bare string at
    ``cfg["model"]``).
    """
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model: managed/bare\n")
    cfg = load_config()
    assert cfg_get(cfg, "model", "default") == "managed/bare"


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("case", ["user_ref", "managed_ref", "namespace_override", "managed_inline", "user_opt_out", "managed_opt_out", "managed_only"])
def test_model_presets_resolve_across_managed_layers(homes, strict, case):
    import yaml
    from hermes_cli.config import load_config, load_config_readonly_strict

    home, managed = homes
    route = {"provider": "custom:fixture", "model": "managed-model", "reasoning_effort": False,
             "fallbacks": [{"provider": "custom:backup", "model": "backup-model"}]}
    user = {"model": {"model_preset": "route"}}
    admin = {"model_presets": {"route": route}}
    if case in {"managed_ref", "managed_opt_out"}:
        user = {"model_presets": {"route": route},
                "model": {"provider": "custom:user", "default": "user-model", "context_length": 12345},
                "agent": {"reasoning_effort": "high"}}
        admin = {"model": {"model_preset": "route"}}
    elif case == "namespace_override":
        user["model_presets"] = {"route": {**route, "model": "user-model", "reasoning_effort": "high"}}
    elif case == "managed_inline":
        user["model_presets"] = {"route": {**route, "model": "user-model"}}
        admin = {"model": {"provider": "custom:fixture", "default": "managed-model"},
                 "agent": {"reasoning_effort": False}, "fallback_providers": route["fallbacks"]}
    elif case == "managed_only":
        user = {}
        admin["model"] = {"model_preset": "route"}
    if case == "user_opt_out":
        user["model"]["fallbacks"] = []
    elif case == "managed_opt_out":
        admin["model"]["fallbacks"] = []
    if case != "managed_only":
        _write(home / "config.yaml", yaml.safe_dump(user))
    _write(managed / "config.yaml", yaml.safe_dump(admin))
    loaded = (load_config_readonly_strict if strict else load_config)()
    assert loaded["model"]["provider"] == "custom:fixture"
    assert loaded["model"]["default"] == "managed-model"
    assert "model_preset" not in loaded["model"]
    assert loaded["agent"]["reasoning_effort"] is False
    assert loaded["fallback_providers"] == ([] if case.endswith("opt_out") else route["fallbacks"])
    if case in {"managed_ref", "managed_opt_out"}:
        assert loaded["model"]["context_length"] == 12345


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("layer", ["user", "managed"])
def test_managed_preset_layers_reject_authored_route_conflicts(homes, strict, layer):
    import yaml
    from hermes_cli.config import load_config, load_config_readonly_strict
    from hermes_cli.model_presets import ModelPresetError

    home, managed = homes
    _write(managed / "config.yaml", yaml.safe_dump({
        "model_presets": {"route": {"provider": "custom:fixture", "model": "fixture"}}}))
    target = home if layer == "user" else managed
    body = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8")) if layer == "managed" else {}
    body["model"] = {"model_preset": "route", "provider": "custom:conflict"}
    _write(target / "config.yaml", yaml.safe_dump(body))
    with pytest.raises(ModelPresetError, match="cannot be combined"):
        (load_config_readonly_strict if strict else load_config)()


@pytest.mark.parametrize("strict", [False, True])
def test_shared_preset_namespace_expands_environment_once(homes, monkeypatch, strict):
    from hermes_cli.config import load_config, load_config_readonly_strict
    home, managed = homes
    monkeypatch.setenv("PRESET_MODEL", "${NESTED_MODEL}")
    monkeypatch.setenv("NESTED_MODEL", "must-not-expand")
    _write(home / "config.yaml", "model: {model_preset: route}\n")
    _write(managed / "config.yaml", "model_presets: {route: {provider: 'custom:fixture', model: '${PRESET_MODEL}'}}\n")
    load = load_config_readonly_strict if strict else load_config
    assert load()["model"]["default"] == "${NESTED_MODEL}"
    monkeypatch.setenv("PRESET_MODEL", "changed-model")
    assert load()["model"]["default"] == "changed-model"
