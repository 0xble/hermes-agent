"""Shared fixtures for alias inheritance consumer regressions."""
import json
from copy import deepcopy
from unittest.mock import MagicMock, patch
import pytest
import hermes_yaml as yaml
from hermes_cli import config as config_mod
from tests.tools.test_delegate import _make_mock_parent
from tools import delegate_tool as delegate

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
        "model_aliases": {
            "uncensored": {"provider": "custom:venice", "model": VENICE},
            "worker": {
                "provider": "custom:worker",
                "model": "worker-model",
                "reasoning_effort": "low",
            },
        },
        "delegation": {
            "model": SOL,
            "provider": "custom:openai-proxy",
            "base_url": "https://pinned.test/v1",
            "api_key": "test-pinned-key",
            "reasoning_effort": "high",
            "fallback_providers": PINNED_CHAIN,
            "request_overrides": {"temperature": 0.2},
            "max_spawn_depth": 3,
            "inherit_parent_aliases": ["uncensored"],
        },
    }


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    config_mod._RAW_CONFIG_CACHE.clear()
    path = tmp_path / "config.yaml"

    def write(raw):
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    write(_authored())
    yield write
    config_mod._RAW_CONFIG_CACHE.clear()


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
