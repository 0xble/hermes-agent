from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_control_callback_unauthorized_does_not_resolve():
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = object.__new__(TelegramAdapter)
    adapter._callback_authorized = AsyncMock(return_value=False)
    query = SimpleNamespace(from_user=SimpleNamespace(id=99))
    query.answer = AsyncMock()
    with patch("hermes_cli.session_controls.resolve_request") as resolve:
        await adapter._handle_session_control_callback(query, "ctl:a:abc123", {})
    resolve.assert_not_called()
    query.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_callback_resolves_and_edits_card():
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = object.__new__(TelegramAdapter)
    adapter._callback_authorized = AsyncMock(return_value=True)
    adapter._edit_md_quiet = AsyncMock()
    query = SimpleNamespace(from_user=SimpleNamespace(id=99, first_name="Brian"), answer=AsyncMock())
    record = {"status": "applied"}
    with patch("hermes_cli.session_controls.resolve_request", return_value=record) as resolve:
        await adapter._handle_session_control_callback(query, "ctl:a:abc123", {})
    resolve.assert_called_once_with("abc123", "approve", "99")
    query.answer.assert_awaited_once()
    adapter._edit_md_quiet.assert_awaited_once()
