"""Validation and authored-config round trips for parent-dependent child routes."""

from copy import deepcopy

import pytest
import hermes_yaml as yaml

from hermes_cli.model_presets import ModelPresetError, expand_model_presets


def _config(by_parent) -> dict:
    return {
        "model_presets": {
            "parent": {"provider": "custom:parent", "model": "parent-model"},
            "alias": {"provider": "other", "model": "parent-model"},
            "worker": {
                "provider": "custom:worker",
                "model": "worker-model",
                "reasoning_effort": "high",
                "fallbacks": [
                    {"provider": "other", "model": "backup", "reasoning_effort": "low"}
                ],
            },
        },
        "delegation": {"model_preset": "worker", "by_parent": by_parent},
    }


def test_expands_parent_keys_and_target_routes_without_mutation():
    authored = _config(
        {
            "@parent": "inherit",
            "literal-model": "@worker",
            "@cf/vendor/model": "inherit",
        }
    )
    before = deepcopy(authored)
    expanded = expand_model_presets(authored)["delegation"]["by_parent"]
    assert expanded["parent-model"] == "inherit"
    from hermes_cli.model_presets import delegation_runtime_config

    assert expanded["literal-model"] == "@worker"
    runtime = delegation_runtime_config(expand_model_presets(authored))
    assert runtime["by_parent"]["literal-model"] == {
        "provider": "custom:worker",
        "model": "worker-model",
        "reasoning_effort": "high",
        "fallback_providers": [
            {"provider": "other", "model": "backup", "reasoning_effort": "low"}
        ],
    }
    assert expanded["@cf/vendor/model"] == "inherit"
    assert authored == before


@pytest.mark.parametrize(
    ("by_parent", "fragment"),
    [
        (None, "must be a mapping"),
        ([], "must be a mapping"),
        ({"@missing": "inherit"}, "unknown preset 'missing'"),
        ({"literal": "@missing"}, "unknown preset 'missing'"),
        ({"literal": "worker"}, "value must be"),
        ({"literal": {"model": "worker-model"}}, "value must be"),
        ({"literal": None}, "value must be"),
        ({"literal": False}, "value must be"),
        ({"": "inherit"}, "parent model must be"),
        ({12: "inherit"}, "parent model must be"),
        ({"@parent": "inherit", "parent-model": "@worker"}, "duplicate parent model"),
        ({"@parent": "inherit", "@alias": "inherit"}, "duplicate parent model"),
    ],
)
def test_invalid_parent_routes_fail_loudly(by_parent, fragment):
    with pytest.raises(ModelPresetError, match=fragment):
        expand_model_presets(_config(by_parent))


@pytest.mark.parametrize("strip_defaults", [True, False])
def test_config_save_preserves_authored_map(tmp_path, monkeypatch, strip_defaults):
    from hermes_cli import config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    raw = _config({"@parent": "inherit", "literal": "@worker"})
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    loaded = config.load_config()
    loaded["delegation"]["max_iterations"] = 71
    config.save_config(loaded, strip_defaults=strip_defaults)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["delegation"]["by_parent"] == raw["delegation"]["by_parent"]
    assert saved["delegation"]["model_preset"] == "worker"
    assert saved["delegation"]["max_iterations"] == 71
    assert config.load_config()["delegation"]["by_parent"]["parent-model"] == "inherit"


@pytest.mark.parametrize("edit", ["add", "change", "delete"])
def test_editing_parent_map_preserves_other_authored_references(
    tmp_path, monkeypatch, edit
):
    from hermes_cli import config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    raw = _config({"@parent": "inherit", "literal": "@worker"})
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    loaded = config.load_config()
    actual = loaded["delegation"]["by_parent"]
    if edit == "add":
        actual["new-model"] = "inherit"
    elif edit == "change":
        actual["parent-model"] = "@worker"
    else:
        actual.pop("parent-model")
    config.save_config(loaded)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["delegation"]["by_parent"]
    assert saved["literal"] == "@worker"
    if edit == "change":
        assert saved["@parent"] == "@worker"
    if edit == "delete":
        assert "@parent" not in saved
    assert config.load_config()["delegation"]["by_parent"]["literal"] == "@worker"


def test_repeated_expansion_and_fallback_resolution_accept_preset_targets():
    from hermes_cli.fallback_config import get_fallback_chain

    raw = _config({"@parent": "@worker"})
    raw["model"] = "@worker"
    expanded = expand_model_presets(raw)
    assert expand_model_presets(expanded) == expanded
    assert get_fallback_chain(expanded) == get_fallback_chain(raw)
    assert yaml.safe_load(yaml.safe_dump(expanded)) == expanded


def test_cli_initialization_accepts_parent_preset_target(tmp_path, monkeypatch):
    import cli
    from hermes_cli import config
    from hermes_cli.cli_config_load import load_cli_config
    from hermes_cli.cli_init_mixin import CLIInitMixin

    path = tmp_path / "config.yaml"
    raw = _config({"@parent": "@worker"})
    raw["model"] = "@worker"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setattr(cli, "CLI_CONFIG", load_cli_config())
    init = CLIInitMixin()
    init.model = "worker-model"
    init._init_prompt_and_reasoning(None)
    assert init._fallback_model == [
        {"provider": "other", "model": "backup", "reasoning_effort": "low"}
    ]


@pytest.mark.parametrize(
    "by_parent",
    [{"@missing": "inherit"}, {"@parent": "inherit", "parent-model": "inherit"}],
)
def test_delegate_loader_does_not_swallow_validation_errors(
    tmp_path, monkeypatch, by_parent
):
    from tools.delegate_tool_config import _load_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(_config(by_parent)), encoding="utf-8"
    )
    with pytest.raises(ModelPresetError):
        _load_config()
