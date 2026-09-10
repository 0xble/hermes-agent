import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.review_status_migration import retire_legacy_review_statuses


def _legacy(message_id):
    return {
        "source": {"platform": "telegram", "chat_id": "42", "thread_id": "8"},
        "message_id": message_id,
    }


@pytest.mark.asyncio
async def test_legacy_review_status_migration_deletes_tracked_message_and_removes_cache(tmp_path):
    path = tmp_path / "cache" / "review-statuses.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"review": _legacy("status-1"), "already-gone": _legacy(None)}))
    adapter = SimpleNamespace(delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda source: adapter if source.platform is Platform.TELEGRAM else None)

    await retire_legacy_review_statuses(runner, home=tmp_path)

    adapter.delete_message.assert_awaited_once_with("42", "status-1")
    assert not path.exists()


@pytest.mark.asyncio
async def test_legacy_review_status_migration_retains_only_failed_or_unroutable_deletions(tmp_path):
    path = tmp_path / "cache" / "review-statuses.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "failed": _legacy("status-1"),
        "invalid": {"source": {"platform": "discord", "chat_id": "42"}, "message_id": "status-2"},
    }))
    adapter = SimpleNamespace(delete_message=AsyncMock(return_value=SendResult(success=False)))
    runner = SimpleNamespace(_adapter_for_source=lambda source: adapter if source.platform is Platform.TELEGRAM else None)

    await retire_legacy_review_statuses(runner, home=tmp_path)

    assert json.loads(path.read_text()) == {
        "failed": _legacy("status-1"),
        "invalid": {"source": {"platform": "discord", "chat_id": "42"}, "message_id": "status-2"},
    }
