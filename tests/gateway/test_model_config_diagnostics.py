"""Model error replies must not expose operator-only config diagnostics."""
import pytest

from gateway.slash_commands_model import GatewayModelCommandsMixin, _ModelSwitchContext


@pytest.mark.asyncio
@pytest.mark.parametrize("document,private", [
    ("model: '@private-preset-name-xyz'\n", "private-preset-name-xyz"),
    ("timezone: PRIVATE/AUTHORED_ZONE\n", "PRIVATE/AUTHORED_ZONE"),
])
async def test_unknown_provider_model_reply_keeps_invalid_config_detail_local(
    tmp_path, monkeypatch, document, private,
):
    from hermes_cli import auth, config, model_switch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(document, encoding="utf-8")
    monkeypatch.setattr(model_switch, "resolve_provider_full", lambda *args: None)
    ctx = _ModelSwitchContext(
        session_key="fixture", source=None, config_path=tmp_path / "config.yaml",
        persist_global=False,
    )
    result, reply = await GatewayModelCommandsMixin()._perform_model_switch(
        ctx, "fixture-model", "bogusprov", None,
    )
    assert result is None
    assert reply is not None
    assert "Unknown provider" in reply and "hermes doctor" in reply
    assert private not in reply
    assert private not in auth._get_config_hint_for_unknown_provider("bogusprov")
    [issue] = config.validate_config_structure()
    assert private not in issue.message
    assert private in issue.local_detail


@pytest.mark.parametrize("builder", ["model", "auth"])
def test_unknown_provider_hints_do_not_copy_structural_issue_text(monkeypatch, builder):
    from hermes_cli import auth, config, model_switch

    monkeypatch.setattr(config, "validate_config_structure", lambda: [
        config.ConfigIssue("warning", "private authored value", "private repair hint"),
    ])
    hint = (model_switch._unknown_provider_message if builder == "model"
            else auth._get_config_hint_for_unknown_provider)("bogusprov")
    assert "hermes doctor" in hint
    assert "private" not in hint
