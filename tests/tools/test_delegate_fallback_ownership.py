"""A child owns a fallback chain only when its route declares one."""

from types import SimpleNamespace

import pytest

from tools.delegate_tool_config import _resolve_child_runtime

_FALLBACK = [{"provider": "custom:claude-proxy", "model": "claude-sonnet-5-5"}]


def _owned(routing_cfg):
    parent = SimpleNamespace(model="gpt-6.2-sol", provider="custom:openai-proxy",
                             base_url="http://127.0.0.1:8317/v1", api_mode="codex_responses", acp_args=[])
    result = _resolve_child_runtime(
        parent, {}, "key", model="gpt-6.2-sol", override_provider=None,
        override_base_url=None, override_api_key=None, override_api_mode=None,
        override_acp_command=None, override_acp_args=None, routing_cfg=routing_cfg,
    )
    return result["fallback_chain_owned"]


@pytest.mark.parametrize(("routing_cfg", "owned"), [
    ({"model": "gpt-6.2-sol", "fallback_providers": _FALLBACK}, True),
    ({"model": "gpt-6.2-sol", "fallback_providers": None}, False),
    ({"model": "gpt-6.2-sol"}, False),
])
def test_only_a_declared_chain_is_owned(routing_cfg, owned):
    assert _owned(routing_cfg) is owned
