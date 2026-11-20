"""TUI agent startup distinguishes explicit model pins from inherited defaults."""

import pytest

from tui_gateway import server


@pytest.mark.parametrize(
    "pin,same_model_spare", [(None, False), ("session", False), ("session", True),
                            ("launch", False), ("launch", True)],
)
def test_make_agent_uses_eligible_fallback_on_auth_error(monkeypatch, pin, same_model_spare):
    """A default can switch models; a session or launch pick can only switch routes."""
    import types

    from hermes_cli.auth import AuthError

    captured = {}
    attempted = []
    original_error = AuthError("No Codex credentials stored")

    def fake_resolve(**kwargs):
        attempted.append(kwargs)
        if kwargs.get("requested") in (None, "openai-codex"):
            raise original_error
        return {"provider": kwargs["requested"], "api_key": "fb-tok", "base_url": "https://fallback.invalid/v1"}

    def fake_agent(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(model=kwargs.get("model"))

    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    if pin == "launch":
        monkeypatch.setenv("HERMES_MODEL", "gpt-5.5")
        monkeypatch.setenv("HERMES_TUI_PROVIDER", "openai-codex")
    chain = [{"provider": "deepseek", "model": "deepseek-v4-pro"}]
    if same_model_spare:
        chain.append({"provider": "openrouter", "model": "gpt-5.5"})
    monkeypatch.setattr(
        server, "_load_cfg", lambda: {
            "model": {"default": "gpt-5.5", "provider": "openai-codex"},
            "fallback_providers": chain,
        },
    )
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve)
    monkeypatch.setattr("run_agent.AIAgent", fake_agent)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *_a, **_kw: ["file"])
    monkeypatch.setattr(server, "_get_db", lambda: None)

    model_override = {
        "model": "gpt-5.5",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": "stale-codex-token",
    } if pin == "session" else None
    if pin and not same_model_spare:
        with pytest.raises(AuthError) as exc_info:
            server._make_agent("sid", "session-key", model_override=model_override)
        assert exc_info.value is original_error
        assert len(attempted) == 1
        assert not captured
        return

    agent = server._make_agent("sid", "session-key", model_override=model_override)
    expected_model = "gpt-5.5" if pin else "deepseek-v4-pro"
    expected_provider = "openrouter" if pin else "deepseek"
    assert len(attempted) == 2
    assert attempted[-1] == {"requested": expected_provider, "target_model": expected_model}
    assert agent.model == expected_model
    assert captured["model"] == expected_model
    assert agent._preflight_fallback == {
        "requested_model": "gpt-5.5", "fallback_model": expected_model,
        "fallback_reason": "credentials_unavailable",
    }
    assert captured["model_pinned"] is bool(pin)
    assert captured["provider"] == expected_provider
    assert captured["base_url"] == "https://fallback.invalid/v1"
    assert captured["api_key"] == "fb-tok"
