import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.missed_message_ack import run_missed_message_ack
from gateway.config import GatewayConfig


class DB:
    def __init__(self):
        self.values = {}
        self.claims = {}

    async def get_missed_message_ack_state(self, kind, *parts):
        return self.values.get((kind, *parts))

    async def set_missed_message_ack_state(self, kind, value, *parts):
        self.values[(kind, *parts)] = value

    async def claim_missed_message_ack(self, platform, conversation, ts):
        key = (platform, conversation, ts)
        if self.claims.get(key) == "sent":
            return False
        self.claims[key] = "reserved"
        return True

    async def mark_missed_message_ack_sent(self, platform, conversation, ts):
        self.claims[(platform, conversation, ts)] = "sent"

    async def release_missed_message_ack(self, platform, conversation, ts):
        self.claims.pop((platform, conversation, ts), None)


class Adapter:
    def __init__(self, records):
        self.records = records
        self.sent = []

    async def fetch_missed_inbound(self, since, until):
        return list(self.records)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SimpleNamespace(success=True)


def runner(adapter, db, enabled=True):
    db.values[("run",)] = 1000
    return SimpleNamespace(
        config=SimpleNamespace(missed_message_ack={"enabled": enabled, "text": "offline"}),
        adapters={"slack": adapter}, _session_db=db, _background_tasks=set(),
    )


@pytest.fixture(autouse=True)
def _enable_feature_for_unit_tests(monkeypatch):
    monkeypatch.setattr("gateway.missed_message_ack._test_mode", lambda: False)


def records(count=1, conversation="D1", start=1500):
    return [
        {"platform": "slack", "conversation": conversation, "ts": str(start + i), "thread_ts": str(start + i)}
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_missed_dm_ack_once_and_existing_dedupe():
    db = DB()
    adapter = Adapter(records(start=1999))
    gw = runner(adapter, db)
    await run_missed_message_ack(gw, now=2000)
    await run_missed_message_ack(gw, now=2001)
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0:3] == ("D1", "offline", "1999")


@pytest.mark.asyncio
async def test_cap_posts_one_summary_per_conversation():
    db = DB()
    adapter = Adapter(records(25, conversation="C1"))
    await run_missed_message_ack(runner(adapter, db), now=2000)
    assert len(adapter.sent) == 21
    assert adapter.sent[-1][1] == "I was offline and missed 5 messages here. Ask again here if you still need it."


@pytest.mark.asyncio
async def test_errors_are_swallowed():
    class Broken(Adapter):
        async def fetch_missed_inbound(self, since, until):
            raise RuntimeError("mock API failure")

    gw = runner(Broken([]), DB())
    await run_missed_message_ack(gw, now=2000)


@pytest.mark.asyncio
async def test_disabled_does_not_fetch_or_send():
    adapter = Adapter(records())
    await run_missed_message_ack(runner(adapter, DB(), enabled=False), now=2000)
    assert not adapter.sent


def test_config_serializes_and_defaults():
    cfg = GatewayConfig.from_dict({"gateway": {"missed_message_ack": {"enabled": False, "text": "later"}}})
    assert cfg.missed_message_ack_enabled is False
    assert cfg.missed_message_ack_text == "later"
    assert cfg.to_dict()["missed_message_ack"] == {"enabled": False, "text": "later"}
