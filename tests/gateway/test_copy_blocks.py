from __future__ import annotations

from types import SimpleNamespace
import logging

import pytest

from gateway.copy_blocks import extract_copy_blocks
from gateway.platforms.base import BasePlatformAdapter


def test_extract_copy_blocks_preserves_body_and_order() -> None:
    text = "before\n[[copy]]\n*exact*  \nline `x`\nMEDIA:/tmp/literal.txt\n[[/copy]]\nafter\n[[copy]]\ntwo\n[[/copy]]"
    remaining, blocks = extract_copy_blocks(text)
    assert remaining == "before\nafter\n"
    assert blocks == ["*exact*  \nline `x`\nMEDIA:/tmp/literal.txt", "two"]


def test_extract_copy_blocks_fences_outside_are_literal_but_inside_are_body() -> None:
    text = "```\n[[copy]]\nliteral\n[[/copy]]\n```\n[[copy]]\n```\nbody\n```\n[[/copy]]"
    remaining, blocks = extract_copy_blocks(text)
    assert remaining == "```\n[[copy]]\nliteral\n[[/copy]]\n```\n"
    assert blocks == ["```\nbody\n```"]


def test_extract_copy_blocks_malformed_empty_and_stray_close() -> None:
    assert extract_copy_blocks("x\n[[/copy]]\ny")[0] == "x\ny"
    assert extract_copy_blocks("[[copy]]\n  \n[[/copy]]") == ("", [])
    assert extract_copy_blocks("prefix\n[[copy]]\nbody") == ("prefix\n", ["body"])


def test_extract_copy_blocks_is_idempotent() -> None:
    text = "reply\n[[copy]]\ncode\n[[/copy]]\n"
    first = extract_copy_blocks(text)
    assert extract_copy_blocks(first[0]) == (first[0], [])


class _FakeAdapter(BasePlatformAdapter):
    @property
    def name(self):
        return "fake"

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SimpleNamespace(success=True)


@pytest.mark.asyncio
async def test_copy_blocks_use_final_ledger_in_source_order_without_sleep(caplog) -> None:
    adapter = object.__new__(_FakeAdapter)
    calls = []
    caplog.set_level(logging.DEBUG, logger="gateway.platforms.base")

    async def send_final_ledgered(event, session_key, content, metadata, **kwargs):
        calls.append((content, metadata.copy(), kwargs))
        return SimpleNamespace(success=True, message_id=None), adapter

    adapter.send_final_ledgered = send_final_ledgered
    results = []
    await adapter._send_copy_blocks(
        SimpleNamespace(source=SimpleNamespace(chat_id="chat")),
        "session",
        ["first *literal*", "second _literal_"],
        {"notify": True},
        results.append,
    )
    assert [call[0] for call in calls] == ["first *literal*", "second _literal_"]
    assert all(call[1]["copy_block"] and call[1]["plain"] for call in calls)
    assert all(result.success for result in results)
    assert any("inter-message start gap:" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_copy_block_failure_is_recorded_and_does_not_disappear() -> None:
    adapter = object.__new__(_FakeAdapter)
    results = []
    attempts = 0

    async def send_final_ledgered(event, session_key, content, metadata, **kwargs):
        nonlocal attempts
        attempts += 1
        return SimpleNamespace(success=attempts == 1, error=None if attempts == 1 else "blocked"), adapter

    adapter.send_final_ledgered = send_final_ledgered
    await adapter._send_copy_blocks(
        SimpleNamespace(source=SimpleNamespace(chat_id="chat")), "session", ["ok", "failed"], {}, results.append)
    assert [result.success for result in results] == [True, False]
    assert attempts == 2


def test_extract_copy_blocks_empty_and_none_inputs() -> None:
    assert extract_copy_blocks("") == ("", [])
    assert extract_copy_blocks("plain reply") == ("plain reply", [])
