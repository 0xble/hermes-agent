"""Regression tests for raw-input recovery identity."""

import pytest

from agent.message_metadata import RAW_INPUT_DIGEST, raw_input_digest, row_carries_raw_input
from gateway.run import GatewayRunner
from gateway.run_pending_recovery import input_already_durable


def _row(raw_text=None):
    row = {"role": "user", "content": "[Alice] hello\n(reply context)"}
    if raw_text is not None:
        row["display_metadata"] = {RAW_INPUT_DIGEST: raw_input_digest(raw_text)}
    return row


def test_prepared_row_matches_same_raw_text_but_not_reused_id_new_text():
    prepared = _row("hello")
    assert row_carries_raw_input(prepared, raw_input_digest("hello"))
    assert not row_carries_raw_input(prepared, raw_input_digest("different"))


def test_legacy_row_falls_back_to_id_only():
    assert row_carries_raw_input(_row(), raw_input_digest("anything"))


def test_restart_dedupe_uses_digest_and_legacy_id_fallback():
    class Store:
        def __init__(self, row):
            self.row = row
            self.calls = []

        def has_platform_message_id(self, session_id, message_id, **kwargs):
            self.calls.append(kwargs)
            return row_carries_raw_input(self.row, kwargs.get("raw_input_digest"))

    same = Store(_row("hello"))
    assert input_already_durable(same, "s", "tg-1", "hello")
    assert same.calls[-1]["raw_input_digest"] == raw_input_digest("hello")

    reused = Store(_row("hello"))
    assert not input_already_durable(reused, "s", "tg-1", "new text")

    legacy = Store(_row())
    assert input_already_durable(legacy, "s", "tg-1", "new text")


@pytest.mark.asyncio
async def test_proxy_recovery_uses_raw_digest_not_prepared_content(monkeypatch):
    class Store:
        def __init__(self, rows):
            self.rows = rows

        async def has_platform_message_id(self, session_id, message_id, *, raw_input_digest=None):
            return any(
                row.get("platform_message_id") == message_id
                and row_carries_raw_input(row, raw_input_digest)
                for row in self.rows
            )

        async def append_to_transcript(self, session_id, row, **kwargs):
            self.rows.append(dict(row))

    runner = object.__new__(GatewayRunner)
    existing = _row("hello")
    existing["platform_message_id"] = "tg-1"
    store = Store([existing])
    monkeypatch.setattr(GatewayRunner, "async_session_store", property(lambda _self: store))

    # The persisted content is gateway-prepared, but the digest is the raw event text.
    assert await runner._hmwa_persist_proxy_recovery_user_row(
        session_id="s", message="[Alice] hello (reply context)", inbound_message_id="tg-1",
        recovery_id=None, recovery_session_key="k", persist_user_message=None,
        persist_user_timestamp=1.0, persist_user_display_kind=None,
        persist_user_display_metadata={RAW_INPUT_DIGEST: raw_input_digest("hello")},
    )
    assert len(store.rows) == 1
    assert await runner._hmwa_persist_proxy_recovery_user_row(
        session_id="s", message="[Alice] new", inbound_message_id="tg-1",
        recovery_id=None, recovery_session_key="k", persist_user_message=None,
        persist_user_timestamp=2.0, persist_user_display_kind=None,
        persist_user_display_metadata={RAW_INPUT_DIGEST: raw_input_digest("new")},
    )
    assert len(store.rows) == 2
