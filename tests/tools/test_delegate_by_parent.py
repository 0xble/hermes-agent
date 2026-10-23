"""Parent-dependent routes through real config loading and child construction."""

import json
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
import hermes_yaml as yaml

from hermes_cli import config as config_mod
from tests.tools.test_delegate import _make_mock_parent
from tools import delegate_tool as delegate
from tools.delegate_tool_config import _select_routing_cfg

VENICE = "abliteration-abliterated-model-large-v2"
SOL = "gpt-6.1-sol"
PARENT_CHAIN = [{"provider": "openrouter", "model": "parent-fallback"}]
PINNED_CHAIN = [
    {"provider": "openrouter", "model": "worker-fallback", "reasoning_effort": "low"}
]
MEDIUM = {"enabled": True, "effort": "medium"}
HIGH = {"enabled": True, "effort": "high"}


def _authored():
    return {
        "model_presets": {
            "uncensored": {"provider": "custom:venice", "model": VENICE},
            "worker": {
                "provider": "custom:worker",
                "model": "worker-model",
                "reasoning_effort": "low",
                "fallbacks": PINNED_CHAIN,
            },
        },
        "delegation": {
            "model": SOL,
            "provider": "custom:codex-proxy",
            "base_url": "https://pinned.test/v1",
            "api_key": "test-pinned-key",
            "reasoning_effort": "high",
            "fallback_providers": PINNED_CHAIN,
            "request_overrides": {"temperature": 0.2},
            "max_spawn_depth": 3,
            "by_parent": {"@uncensored": "inherit"},
        },
    }


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    path = tmp_path / "config.yaml"

    def write(raw):
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    write(_authored())
    yield write
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def _parent(model=VENICE):
    parent = _make_mock_parent()
    parent.model = model
    parent.provider = parent.requested_provider = "custom:venice"
    parent.base_url = "https://venice.test/v1"
    parent.reasoning_config = dict(MEDIUM)
    parent.reasoning_override = None
    parent._fallback_chain = deepcopy(PARENT_CHAIN)
    parent.acp_command = None
    parent.acp_args = []
    parent.capabilities = None
    parent.service_tier = None
    parent.request_overrides = {"temperature": 0.8}
    return parent


def _spawn(parent, **kwargs):
    child = MagicMock()
    with (
        patch("run_agent.AIAgent", return_value=child) as agent,
        patch.object(delegate, "_run_batch", return_value=json.dumps({"ok": True})),
    ):
        result = json.loads(
            delegate.delegate_task(goal="Check route", parent_agent=parent, **kwargs)
        )
    assert result == {"ok": True}, result
    return agent.call_args.kwargs, child


def _runtime(kwargs):
    # Exclude generated prompt/progress identities, not any route or inference settings.
    return {
        key: value
        for key, value in kwargs.items()
        if key
        not in {
            "ephemeral_system_prompt",
            "thinking_callback",
            "tool_progress_callback",
        }
    }


@pytest.mark.parametrize("background", [False, True])
def test_inherit_uses_live_parent_route_chain_and_reasoning(config_file, background):
    parent = _parent()
    kwargs, _ = _spawn(parent, background=background)
    assert (kwargs["model"], kwargs["provider"], kwargs["base_url"]) == (
        parent.model,
        parent.provider,
        parent.base_url,
    )
    assert kwargs["api_key"] == parent.api_key
    assert kwargs["api_mode"] == parent.api_mode
    assert kwargs["fallback_model"] == PARENT_CHAIN
    assert kwargs["reasoning_config"] == MEDIUM
    assert kwargs["request_overrides"] == parent.request_overrides


@pytest.mark.parametrize("model", [SOL, "claude-opus-4.8", "parent-fallback"])
def test_unmatched_live_model_has_identical_constructor_kwargs(config_file, model):
    parent = _parent(model)
    routed, _ = _spawn(parent)
    raw = _authored()
    raw["delegation"].pop("by_parent")
    config_file(raw)
    original, _ = _spawn(parent)
    assert _runtime(routed) == _runtime(original)
    assert routed["model"] == SOL
    assert routed["base_url"] == "https://pinned.test/v1"
    assert routed["fallback_model"] == PINNED_CHAIN
    assert routed["reasoning_config"] == HIGH


def test_model_switch_and_config_edit_apply_on_next_spawn(config_file):
    parent = _parent()
    assert _spawn(parent)[0]["model"] == VENICE
    parent.model = SOL
    assert _spawn(parent)[0]["model"] == SOL
    parent.model = VENICE
    raw = _authored()
    raw["delegation"]["by_parent"] = {"other-parent": "inherit"}
    config_file(raw)
    assert _spawn(parent)[0]["model"] == SOL
    config_file(_authored())
    assert _spawn(parent)[0]["model"] == VENICE


def test_preset_value_replaces_pinned_route_and_reasoning(config_file):
    raw = _authored()
    raw["delegation"]["by_parent"]["@uncensored"] = "@worker"
    config_file(raw)
    runtime = {
        "provider": "custom",
        "base_url": "https://worker.test/v1",
        "api_key": "test-worker-key",
        "api_mode": "chat_completions",
    }
    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime
    ) as resolve:
        kwargs, _ = _spawn(_parent())
    resolve.assert_called_once_with(
        requested="custom:worker", target_model="worker-model"
    )
    assert (kwargs["model"], kwargs["provider"], kwargs["base_url"]) == (
        "worker-model",
        "custom:worker",
        "https://worker.test/v1",
    )
    assert kwargs["api_key"] == "test-worker-key"
    assert kwargs["fallback_model"] == PINNED_CHAIN
    assert kwargs["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert "temperature" not in kwargs["request_overrides"]


def test_nested_inherit_child_uses_same_route(config_file):
    first, child = _spawn(_parent())
    # Mirror the constructor-owned route state without invoking an inference provider.
    for key in (
        "model",
        "provider",
        "requested_provider",
        "base_url",
        "api_key",
        "api_mode",
        "reasoning_config",
        "request_overrides",
        "acp_command",
        "acp_args",
        "capabilities",
    ):
        setattr(child, key, first[key])
    child._fallback_chain = first["fallback_model"]
    child.reasoning_override = None
    child._session_db = None
    nested, _ = _spawn(child)
    assert nested["model"] == VENICE
    assert nested["base_url"] == first["base_url"]
    assert nested["fallback_model"] == PARENT_CHAIN
    assert nested["reasoning_config"] == MEDIUM


@pytest.mark.parametrize(
    "credentials_cfg",
    [
        {},
        {
            "model": "review-model",
            "base_url": "https://review.test/v1",
            "reasoning_effort": "low",
            "fallback_providers": [],
        },
    ],
)
def test_internal_review_credentials_are_unaffected(config_file, credentials_cfg):
    parent = _parent()
    routed, _ = _spawn(parent, credentials_cfg=credentials_cfg)
    raw = _authored()
    raw["delegation"].pop("by_parent")
    config_file(raw)
    original, _ = _spawn(parent, credentials_cfg=credentials_cfg)
    assert _runtime(routed) == _runtime(original)
    assert routed["reasoning_config"] == HIGH


def test_selection_strips_every_route_key_and_keeps_operational_knobs():
    cfg: dict = {
        key: "pinned"
        for key in (
            "model",
            "provider",
            "base_url",
            "api_key",
            "api_mode",
            "request_overrides",
            "fallback_providers",
            "fallback_chain",
            "reasoning_effort",
            "command",
            "args",
        )
    }
    route_keys = set(cfg)
    cfg.update(
        by_parent={VENICE: "inherit"},
        compression_threshold_tokens=16000,
        max_iterations=50,
    )
    selected = _select_routing_cfg(cfg, _parent())
    assert not route_keys.intersection(selected)
    assert selected["compression_threshold_tokens"] == 16000
    assert selected["max_iterations"] == 50
    assert cfg["model"] == "pinned"
    assert _select_routing_cfg(cfg, _parent("other")) is cfg
