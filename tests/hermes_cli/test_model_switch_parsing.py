"""Single-owner /model argument parsing (hermes_cli.model_switch.parse_model_switch_args)."""


from hermes_cli.model_switch import (
    MODEL_SWITCH_ERR_ONCE_WITH_GLOBAL,
    MODEL_SWITCH_ERROR_TEXT,
    parse_model_switch_args,
)


# ---------------------------------------------------------------------------
# parse_model_switch_args — the ONE parser
# ---------------------------------------------------------------------------


def test_provider_flag_and_scopes():
    req = parse_model_switch_args("sonnet --provider anthropic --global")
    assert req.target == "sonnet"
    assert req.explicit_provider == "anthropic"
    assert req.is_global is True
    assert req.scope == "global"
    assert req.errors == ()

    assert parse_model_switch_args("sonnet").scope == "default"
    assert parse_model_switch_args("sonnet --session").scope == "session"
    assert parse_model_switch_args("sonnet \u2013session").scope == "session"  # iOS/Telegram en-dash
    assert parse_model_switch_args("sonnet --once").scope == "once"
    assert parse_model_switch_args("--refresh").force_refresh is True
    assert parse_model_switch_args("reset --all").is_all is True
    assert parse_model_switch_args("reset \u2013all").is_all is True  # iOS/Telegram en-dash
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212", "\ufe58", "\ufe63", "\uff0d"):
        assert parse_model_switch_args(f"reset {dash}all").is_all is True


def test_cli_rejects_gateway_only_reset_all(monkeypatch):
    from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
    from types import SimpleNamespace
    import sys

    output = []
    monkeypatch.setitem(sys.modules, "cli", SimpleNamespace(_cprint=output.append))
    CLIModelSwitchMixin._handle_model_switch(object(), "/model reset --all")
    assert output == ["  ✗ /model --all is only available in the gateway."]


def test_tui_rejects_gateway_only_reset_all():
    from tui_gateway.model_switch import _switch_request

    try:
        _switch_request("reset --all", None, None)
    except ValueError as exc:
        assert str(exc) == "/model --all is only available in the gateway."
    else:
        raise AssertionError("TUI accepted the gateway-only --all modifier")


def test_once_with_global_conflict():
    req = parse_model_switch_args("sonnet --once --global")
    assert MODEL_SWITCH_ERR_ONCE_WITH_GLOBAL in req.errors
    assert MODEL_SWITCH_ERROR_TEXT[MODEL_SWITCH_ERR_ONCE_WITH_GLOBAL] in req.error_messages()
