"""Topic module for final send failure restart note tests (split to offset FILE_LINES)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from gateway.platforms.base import SendResult

from .test_core import (
    NoteAdapter,
    _pending_store,
    _configure_real_final_delivery,
)


@pytest.mark.asyncio
async def test_final_send_failure_keeps_restart_note_and_pointer(tmp_path):
    adapter = NoteAdapter()
    store, entry, event = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-send-failed")
    _configure_real_final_delivery(adapter, store)
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=False, error="transport down"))

    await adapter._send_final_text(event, entry.session_key, "failed answer", {}, False, 0, lambda _r: None)

    assert adapter.deleted == []
    assert store.get_restart_note(entry.session_key)[3] == "note-send-failed"
