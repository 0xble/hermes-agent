"""A missed payload still belongs to its owner topic, unlike completed work."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import telegram_retire_work as work


@pytest.mark.asyncio
async def test_inventory_retains_missed_cron_owner_decision(monkeypatch):
    origin = {"platform": "telegram", "chat_id": "101", "thread_id": "17"}
    held = {"id": "held", "state": "missed", "enabled": False, "prompt": "payload", "origin": origin}
    completed = {**held, "id": "done", "state": "completed"}
    monkeypatch.setattr("cron.jobs.list_jobs", lambda **kwargs: [held, completed])
    monkeypatch.setattr("tools.async_delegation.retirement_inventory", lambda *args: [])
    monkeypatch.setattr("tools.process_registry.process_registry.list_sessions", lambda **kwargs: [])
    monkeypatch.setattr(work, "locate_command", lambda _: SimpleNamespace(command=None))
    runner = SimpleNamespace(_session_db=SimpleNamespace(get_meta=AsyncMock(return_value=None)))
    source = SimpleNamespace(chat_id="101", thread_id="17", profile="default")
    items = await work.inventory(runner, source, {"session_id": "sid", "session_key": "key"})
    assert [item["id"] for item in items] == ["cron:held"]
    assert items[0]["record"]["prompt"] == "payload"
