"""Ordinary turns and commands need no loop-owner state on standalone mixin hosts."""

from types import SimpleNamespace

import pytest

from gateway.run_goals import GatewayGoalsMixin
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_turn import GatewayTurnMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("host_type", [GatewayTurnMixin, GatewayInboundMixin, GatewayGoalsMixin])
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("metadata", [None, {}, {"other_notification": True}])
async def test_non_loop_events_pass_through_bare_hosts(host_type, internal, metadata):
    host = object.__new__(host_type)
    event = SimpleNamespace(internal=internal, metadata=metadata)
    result = object()
    calls = []

    async def async_handler(value, *, label):
        calls.append((value, label))
        return result

    def sync_handler(value):
        calls.append(value)
        return result

    assert await host._prepare_loop_wake_execution(event, None) is True
    assert await host._invoke_loop_wake_effect(event, async_handler, result, label="async") == (True, result)
    assert host._invoke_loop_wake_sync_effect(event, sync_handler, result) == (True, result)
    assert calls == [(result, "async"), result]
    assert vars(host) == {}
    assert vars(event) == {"internal": internal, "metadata": metadata}
