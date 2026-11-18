"""Intentional supervisor disarm is terminal at the real adapter retry boundary."""

import logging
from unittest.mock import AsyncMock

import pytest

from gateway import run_checkins
from gateway.checkin_delivery import DISARMED, delivery_metadata
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from hermes_cli.checkins import CheckinConfig
from tests.gateway.test_send_retry_hooks import _Adapter
from tests.gateway import test_supervisor_checkins as supervisor_fixtures

fixture = supervisor_fixtures.fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["disable", "remove", "transport-error"])
async def test_retry_wrapper_distinguishes_disarm_from_real_send_failure(fixture, monkeypatch, caplog, refusal):
    f = fixture
    config = CheckinConfig(enabled=refusal != "disable", only_roots=("other",) if refusal == "remove" else ("owner",))
    monkeypatch.setattr(CheckinConfig, "load", classmethod(lambda cls: config))
    failed = SendResult(success=False, error="format rejected")
    adapter = _Adapter([failed, failed])
    fallback = AsyncMock(wraps=adapter._send_plain_fallback)
    adapter._send_plain_fallback = fallback
    run_checkins.install_visible_observer(f.runner, adapter)
    event = MessageEvent(text="pending", source=f.source, internal=True)
    event._checkin_pending_root = "owner"
    with caplog.at_level(logging.WARNING):
        result = await adapter._send_with_retry("42", "pending", metadata=delivery_metadata(event, {}))
    assert result.success is False
    if refusal == "transport-error":
        assert result.error == "format rejected"
        fallback.assert_awaited_once()
        assert len(adapter.sent) == 2
        assert any(record.levelno >= logging.ERROR and "Fallback send also failed" in record.getMessage()
                   for record in caplog.records)
    else:
        assert result.error == DISARMED and result.pre_send
        assert adapter.sent == []
        fallback.assert_not_awaited()
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
