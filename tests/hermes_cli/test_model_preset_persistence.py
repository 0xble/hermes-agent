"""Concrete selections must replace, not corrupt, authored preset references."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import hermes_yaml as yaml
from hermes_cli.model_switch import ModelSwitchResult, persist_model_selection


@pytest.fixture
def concrete_pick():
    return ModelSwitchResult(
        success=True, new_model="gpt-5.5", target_provider="openai",
        base_url="https://api.openai.com/v1", api_mode="responses",
        api_key="ephemeral-not-for-config",
    )


@pytest.fixture(params=["canonical", "string", "default", "model"])
def preset_config(request, tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    header = "model_presets:\n  fast:\n    provider: openrouter\n    model: fast-model\n"
    if request.param == "string":
        model = "model: '@fast'\n"
    else:
        key, value = {"canonical": ("model_preset", "fast"), "default": ("default", "'@fast'"), "model": ("model", "'@fast'")}[request.param]
        model = (
            f"model:\n  {key}: {value}\n  # Keep my route policy.\n  max_context_chars: 12345\n"
            "  model_slots:\n    fast: 'off'  # Keep my slot.\n"
            '  model_fallback: ["no"]  # Keep my fallback.\n'
        )
    path = home / "config.yaml"
    path.write_text(header + model + "display:\n  compact: true\n", encoding="utf-8")
    return path, request.param


def assert_concrete_reload(path, form):
    from hermes_cli.config import load_config
    raw_text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(raw_text)
    assert "model_preset" not in raw["model"]
    assert raw["model"].get("model") != "@fast"
    assert raw["model"]["default"] == "gpt-5.5"
    assert raw["model"]["provider"] == "openai"
    assert raw["model_presets"]["fast"]["model"] == "fast-model"
    assert raw["display"]["compact"] is True
    assert "ephemeral-not-for-config" not in raw_text
    if form != "string":
        assert raw["model"]["max_context_chars"] == 12345
        assert raw["model"]["model_slots"] == {"fast": "off"}
        assert raw["model"]["model_fallback"] == ["no"]
        assert "# Keep my route policy." in raw_text
        assert "fast: 'off'  # Keep my slot." in raw_text
        assert 'model_fallback: ["no"]  # Keep my fallback.' in raw_text
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_config()
    assert loaded["model"]["default"] == "gpt-5.5"
    assert loaded["model"]["provider"] == "openai"
    return loaded


def test_shared_writer_replaces_every_reference_form(preset_config, concrete_pick, monkeypatch):
    """Readers at every real file replacement see one complete, valid selection."""
    from pathlib import Path
    from hermes_cli.config import load_config
    from hermes_cli.model_presets import ModelPresetError
    import utils

    path, form = preset_config
    snapshots = []

    def read_snapshot():
        try:
            model = load_config()["model"]
            snapshots.append(tuple(model.get(key) for key in ("default", "provider", "base_url", "api_mode")))
        except ModelPresetError as exc:
            snapshots.append(str(exc))

    read_snapshot()
    assert snapshots[0][0] == "fast-model"
    replace = utils.os.replace

    def replace_and_read(source, target):
        replace(source, target)
        if Path(target) == path:
            read_snapshot()

    monkeypatch.setattr(utils.os, "replace", replace_and_read)
    persist_model_selection(concrete_pick, config_path=path)
    assert snapshots[1:] == [(
        "gpt-5.5", "openai", "https://api.openai.com/v1", "responses",
    )], snapshots
    assert_concrete_reload(path, form)


@pytest.mark.parametrize("surface", ["gateway", "cli", "tui"])
def test_model_command_commit_paths_reload(preset_config, concrete_pick, monkeypatch, surface):
    """Real caller seams invoke the same real writer against a scratch config."""
    path, form = preset_config
    if surface == "gateway":
        from gateway.slash_commands_model import _persist_model_switch_to_config
        asyncio.run(_persist_model_switch_to_config(concrete_pick, path))
    elif surface == "cli":
        from hermes_cli import cli_model_switch_mixin as cli_switch
        import cli
        monkeypatch.setattr(cli, "_cprint", lambda *a, **kw: None)
        monkeypatch.setattr(cli_switch, "_print_switch_summary", lambda *a, **kw: None)
        owner = SimpleNamespace(model="old-model", provider="openai", is_slash_worker=True,
                                _stage_and_swap_model=Mock(return_value=True))
        cli_switch._commit_model_switch(owner, concrete_pick, persist_global=True)
        owner._stage_and_swap_model.assert_called_once_with(concrete_pick, "old-model")
    else:
        from tui_gateway import model_switch as tui
        monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: concrete_pick)
        monkeypatch.setattr(tui, "_current_model_runtime", lambda *a: ("openai", "old-model", "", ""))
        monkeypatch.setattr(tui, "_expensive_model_confirm", lambda *a: None)
        monkeypatch.setattr(tui, "_session_source", lambda _: "tui", raising=False)
        reply = tui._apply_model_switch("scratch-session", {}, "gpt-5.5 --global")
        assert reply["scope"] == "global"
    assert_concrete_reload(path, form)


def test_unrelated_save_preserves_reference(preset_config):
    from hermes_cli.config import load_config, save_config
    path, form = preset_config
    loaded = load_config()
    loaded["display"]["compact"] = False
    save_config(loaded)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = {"canonical": {"model_preset": "fast"}, "string": "@fast", "default": {"default": "@fast"}, "model": {"model": "@fast"}}[form]
    if isinstance(expected, dict):
        for key, value in expected.items():
            assert raw["model"][key] == value
    else:
        assert raw["model"] == expected
    assert load_config()["model"]["default"] == "fast-model"


def test_new_session_unknown_preset_is_best_effort(monkeypatch):
    import cli
    from hermes_cli.cli_session_mixin import _reset_model_to_config_default
    monkeypatch.setattr(cli, "CLI_CONFIG", {"model": {"default": "@missing"}})
    owner = SimpleNamespace(_apply_model_switch=Mock())
    _reset_model_to_config_default(owner, silent=True)
    owner._apply_model_switch.assert_not_called()
