"""Topic icon recovery accepts presentation variants, not unsupported identities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.telegram_topic_sessions import TopicRequestError, _create_icon, topic_icon_id


@pytest.mark.asyncio
async def test_topic_create_and_edit_accept_catalog_presentation_variants():
    adapter = SimpleNamespace(get_forum_topic_icon_options=AsyncMock(return_value=[
        {"emoji": "⚡", "custom_emoji_id": "lightning"},
        {"emoji": "✈️", "custom_emoji_id": "plane"},
    ]))
    assert await _create_icon(adapter, "⚡️") == ("lightning", [])
    assert await topic_icon_id(adapter, "✈") == "plane"


@pytest.mark.asyncio
async def test_unsupported_topic_icon_still_refuses_and_suggests_only_catalog_choices():
    adapter = SimpleNamespace(get_forum_topic_icon_options=AsyncMock(return_value=[
        {"emoji": "👨", "custom_emoji_id": "person"},
        {"emoji": "⚡", "custom_emoji_id": "lightning"},
    ]))
    with pytest.raises(TopicRequestError) as exc:
        await _create_icon(adapter, "👨‍💻")
    text = str(exc.value)
    assert "Unsupported topic icon" in text
    assert "Available icons: 👨 ⚡" in text
    assert "Closest" in text and "👨" in text


@pytest.mark.asyncio
async def test_unrelated_topic_icon_gets_no_closest_suggestion():
    adapter = SimpleNamespace(get_forum_topic_icon_options=AsyncMock(return_value=[
        {"emoji": "👨", "custom_emoji_id": "person"},
        {"emoji": "⚡", "custom_emoji_id": "lightning"},
    ]))
    with pytest.raises(TopicRequestError) as exc:
        await _create_icon(adapter, "🍕")
    assert "Closest" not in str(exc.value)
    assert "Available icons: 👨 ⚡" in str(exc.value)
