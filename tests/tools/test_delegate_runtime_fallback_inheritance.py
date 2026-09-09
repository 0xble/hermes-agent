"""Resolved child model names must not silently pin recovery policy."""
from types import SimpleNamespace

from tools.delegate_tool_config import _resolve_child_runtime


def test_resolved_model_preserves_unpinned_parent_fallback():
    chain = [{"provider": "openai", "model": "recovery-model"}]
    parent = SimpleNamespace(
        model="primary-model", provider="openai", base_url="https://example.invalid/v1",
        _fallback_chain=chain,
    )
    runtime = _resolve_child_runtime(
        parent, {}, None, model=parent.model, override_provider=None,
        override_base_url=None, override_api_key=None, override_api_mode=None,
        override_acp_command=None, override_acp_args=None,
    )
    assert runtime["fallback_model"] == chain
