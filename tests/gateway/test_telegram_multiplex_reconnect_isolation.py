"""Every replacement-adapter egress lane stays with its transport owner."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from plugins.platforms.telegram.adapter import TelegramAdapter


EGRESS = [
    ("send", ("42", "answer"), {}),
    ("edit_message", ("42", "1", "answer"), {"finalize": True}),
    ("delete_message", ("42", "1"), {}),
    ("delete_messages", ("42", ["1", "2"]), {}),
    ("send_typing", ("42",), {}),
]


def _adapter(profile):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.set_owner_profile(profile)
    adapter._bot = object()
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,kwargs", EGRESS)
@pytest.mark.parametrize("primary_name", ["default", "named-primary"])
@pytest.mark.parametrize("secondary", [False, True])
@pytest.mark.parametrize("replacement_present", [True, False])
async def test_retired_adapter_egress_is_profile_scoped_and_fail_closed(
    method, args, kwargs, primary_name, secondary, replacement_present,
):
    owner = "secondary" if secondary else primary_name
    retired = _adapter(owner)
    retired._bot = None
    retired._RECONNECT_WAIT_SECONDS = 0
    primary, second = _adapter(primary_name), _adapter("secondary")
    for adapter in (primary, second):
        setattr(adapter, method, AsyncMock(return_value=SendResult(success=True)))
    runner = object.__new__(GatewayRunner)
    runner._primary_profile_name = primary_name
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {"secondary": {Platform.TELEGRAM: second}}
    expected, other = (second, primary) if secondary else (primary, second)
    if not replacement_present:
        (runner._profile_adapters[owner] if secondary else runner.adapters).clear()
    retired.gateway_runner = runner
    await getattr(retired, method)(*args, **kwargs)
    getattr(other, method).assert_not_awaited()
    if replacement_present:
        getattr(expected, method).assert_awaited_once()
    else:
        getattr(expected, method).assert_not_awaited()


@pytest.mark.parametrize("profiles", [None, {}, {"secondary": {}}])
def test_legacy_runner_never_falls_back_from_missing_secondary(profiles):
    retired, primary = _adapter("secondary"), _adapter("default")
    retired._bot = None
    retired.gateway_runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: primary}, _profile_adapters=profiles,
    )
    assert retired._replacement_telegram_adapter() is None
