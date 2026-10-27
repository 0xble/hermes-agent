"""Regression tests for #25106: CLI `/model <name> --global` must persist
``model.base_url`` and ``model.api_mode`` along with the concrete selection.
A provider switch must not leave the previous endpoint or wire protocol on disk.
"""

import pytest

import hermes_yaml as yaml
from hermes_cli.model_switch import ModelSwitchResult


@pytest.fixture(autouse=True)
def model_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(
        "model:\n  default: old-model\n  provider: copilot\n"
        "  base_url: https://api.githubcopilot.com\n  api_mode: chat_completions\n",
        encoding="utf-8",
    )
    return path


def _saved_model():
    from hermes_cli.config import read_user_config_raw
    return {f"model.{key}": value for key, value in read_user_config_raw()["model"].items()}


def _make_result(*, base_url="https://api.minimax.io/v1", api_mode="chat_completions", provider_changed=True):
    return ModelSwitchResult(
        success=True,
        new_model="MiniMax-M3",
        target_provider="custom:minimax",
        provider_changed=provider_changed,
        api_key="sk-minimax",
        base_url=base_url,
        api_mode=api_mode,
        warning_message="",
        provider_label="MiniMax (custom)",
        resolved_via_alias=False,
        capabilities=None,
        model_info=None,
        is_global=True,
    )


class _StubCLI:
    """Minimum attrs/methods `_handle_model_switch` reads or calls on self."""
    def _stage_and_swap_model(self, result, old_model):
        import cli as cli_mod
        return cli_mod.HermesCLI._stage_and_swap_model(self, result, old_model)

    agent = None
    model = "old-model"
    provider = "copilot"
    requested_provider = "copilot"
    api_key = "sk-old"
    base_url = "https://api.githubcopilot.com"
    api_mode = "chat_completions"
    _explicit_api_key = ""
    _explicit_base_url = ""
    conversation_history = []
    _pending_model_switch_note = ""

    def _confirm_expensive_model_switch(self, result) -> bool:
        return True

    def _confirm_and_apply_cli_model_switch(
        self, result, persist_global, one_turn, custom_provs=None
    ):
        import cli as cli_mod
        return cli_mod.HermesCLI._confirm_and_apply_cli_model_switch(
            self, result, persist_global, one_turn, custom_provs
        )

    def _open_model_picker(self, *a, **k):
        raise AssertionError("picker should not open when a model name is given")


def _run_switch(monkeypatch, tmp_path, result, cmd="/model MiniMax-M3 --global"):
    import cli as cli_mod

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  default: old-model\n  provider: copilot\n"
        "  base_url: https://api.githubcopilot.com\n  api_mode: chat_completions\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("hermes_cli.config.get_config_path", lambda: config_path)
    monkeypatch.setattr(cli_mod, "_cprint", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: result)
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: (_ for _ in ()).throw(RuntimeError("no picker context in test")),
    )
    before = config_path.read_bytes()
    cli_mod.HermesCLI._handle_model_switch(_StubCLI(), cmd)
    return config_path, before


def _run_apply(monkeypatch, result, persist_global=True):
    """Drive the interactive-picker persistence path against the isolated config."""
    import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_cprint", lambda *a, **k: None)
    cli_mod.HermesCLI._apply_model_switch_result(_StubCLI(), result, persist_global)
    return _saved_model()


def test_picker_global_switch_persists_base_url_and_api_mode(monkeypatch):
    saved = _run_apply(monkeypatch, _make_result())
    assert saved["model.default"] == "MiniMax-M3"
    assert saved["model.provider"] == "custom:minimax"
    assert saved["model.base_url"] == "https://api.minimax.io/v1"
    assert saved["model.api_mode"] == "chat_completions"


@pytest.mark.parametrize("base_url,api_mode", [
    ("https://api.minimax.io/v1", "chat_completions"),
    (None, None),
])
def test_global_switch_persists_base_url_and_api_mode(monkeypatch, tmp_path, base_url, api_mode):
    path, _ = _run_switch(monkeypatch, tmp_path, _make_result(base_url=base_url, api_mode=api_mode))
    model = yaml.safe_load(path.read_text(encoding="utf-8"))["model"]

    assert model["default"] == "MiniMax-M3"
    assert model["provider"] == "custom:minimax"
    assert model.get("base_url") == base_url
    assert model.get("api_mode") == api_mode


def test_session_only_switch_does_not_touch_config(monkeypatch, tmp_path):
    path, before = _run_switch(monkeypatch, tmp_path, _make_result(), "/model MiniMax-M3 --session")
    assert path.read_bytes() == before
