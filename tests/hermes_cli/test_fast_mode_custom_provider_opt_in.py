"""``capabilities.fast_mode`` opts a configured custom provider into fast-mode params.

Exercised through the real config loader against a temp ``HERMES_HOME`` (the autouse sandbox), with
the layout the opt-in exists for: one local proxy URL serving two entries, one per transport.
"""

from __future__ import annotations

import pytest
import yaml

from agent.anthropic_adapter import build_anthropic_kwargs
from hermes_cli.models import fast_mode_route_ignored, resolve_fast_mode_overrides
from tools.delegate_tool_config import _resolve_child_request_overrides

PROXY = "http://127.0.0.1:8317/v1"
CODEX_MODEL = "gpt-6-sol"
CLAUDE_MODEL = "claude-opus-5-5"


def _write_providers(codex_fast: bool | None, claude_fast: bool | None) -> None:
    from hermes_constants import get_hermes_home

    def entry(api_mode: str, fast: bool | None) -> dict:
        capabilities = {"concurrent_requests": True}
        if fast is not None:
            capabilities["fast_mode"] = fast
        return {"base_url": PROXY, "api_mode": api_mode, "key_env": "PROXY_KEY", "capabilities": capabilities}

    config = {"providers": {
        "codex-proxy": entry("codex_responses", codex_fast),
        "claude-proxy": entry("anthropic_messages", claude_fast),
    }}
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _claude_kwargs() -> dict:
    return build_anthropic_kwargs(
        model=CLAUDE_MODEL, messages=[{"role": "user", "content": "hi"}], tools=None,
        max_tokens=64, reasoning_config=None, base_url=PROXY, fast_mode=True)


def test_opted_in_codex_proxy_receives_priority_and_the_warning_stops():
    _write_providers(codex_fast=True, claude_fast=None)
    assert resolve_fast_mode_overrides(CODEX_MODEL, provider="custom:codex-proxy", base_url=PROXY) == {
        "service_tier": "priority"}
    assert not fast_mode_route_ignored(CODEX_MODEL, "custom:codex-proxy", PROXY)


def test_opting_in_codex_leaves_the_claude_proxy_on_the_same_url_closed():
    _write_providers(codex_fast=True, claude_fast=None)
    assert resolve_fast_mode_overrides(CLAUDE_MODEL, provider="custom:claude-proxy", base_url=PROXY) is None
    # Runtime resolution can collapse a named route to bare ``custom`` + URL; the URL alone must
    # not borrow the Codex entry's opt-in for an Anthropic-transport request.
    assert resolve_fast_mode_overrides(CLAUDE_MODEL, provider="custom", base_url=PROXY) is None
    assert fast_mode_route_ignored(CLAUDE_MODEL, "custom:claude-proxy", PROXY)
    kwargs = _claude_kwargs()
    assert "speed" not in (kwargs.get("extra_body") or {})


def test_bare_custom_route_follows_the_same_transport_entry():
    _write_providers(codex_fast=True, claude_fast=None)
    assert resolve_fast_mode_overrides(CODEX_MODEL, provider="custom", base_url=PROXY) == {"service_tier": "priority"}


def test_opted_in_claude_proxy_receives_speed_and_the_beta():
    _write_providers(codex_fast=None, claude_fast=True)
    assert resolve_fast_mode_overrides(CLAUDE_MODEL, provider="custom:claude-proxy", base_url=PROXY) == {
        "speed": "fast"}
    kwargs = _claude_kwargs()
    assert kwargs["extra_body"]["speed"] == "fast"
    assert "fast-mode-2026-02-01" in kwargs["extra_headers"]["anthropic-beta"]


@pytest.mark.parametrize("codex_fast", [None, False])
def test_custom_route_without_an_explicit_true_stays_closed(codex_fast):
    _write_providers(codex_fast=codex_fast, claude_fast=None)
    assert resolve_fast_mode_overrides(CODEX_MODEL, provider="custom:codex-proxy", base_url=PROXY) is None
    assert fast_mode_route_ignored(CODEX_MODEL, "custom:codex-proxy", PROXY)


def test_opt_in_never_crosses_model_families():
    """A Codex-transport opt-in cannot carry Anthropic ``speed``, and vice versa."""
    _write_providers(codex_fast=True, claude_fast=True)
    assert resolve_fast_mode_overrides(CLAUDE_MODEL, provider="custom:codex-proxy", base_url=PROXY) is None
    assert resolve_fast_mode_overrides(CODEX_MODEL, provider="custom:claude-proxy", base_url=PROXY) is None


def test_unconfigured_custom_url_and_other_resellers_stay_closed():
    _write_providers(codex_fast=True, claude_fast=True)
    assert resolve_fast_mode_overrides(CODEX_MODEL, provider="custom", base_url="http://127.0.0.1:9999/v1") is None
    assert resolve_fast_mode_overrides(CODEX_MODEL, provider="openrouter", base_url="https://openrouter.ai/api/v1") is None
    assert resolve_fast_mode_overrides(CODEX_MODEL, provider="openai", base_url="https://api.openai.com/v1") == {
        "service_tier": "priority"}


def test_delegated_child_on_an_opted_in_proxy_inherits_priority(monkeypatch):
    from types import SimpleNamespace

    import tools.delegate_tool_config as cfg

    _write_providers(codex_fast=True, claude_fast=None)
    monkeypatch.setattr(cfg, "_inherit_service_tier", lambda: True)
    parent = SimpleNamespace(service_tier="priority", request_overrides={}, provider="custom:claude-proxy",
                             base_url=PROXY, model=CLAUDE_MODEL)
    overrides = _resolve_child_request_overrides(
        parent, child_model=CODEX_MODEL, child_provider="custom:codex-proxy", child_base_url=PROXY,
        explicit_overrides=None, inherit_parent_route=False)
    assert overrides.get("service_tier") == "priority"
    assert "speed" not in overrides
