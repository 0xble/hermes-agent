from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from gateway.missed_message_ack import (
    _ack_one,
    _ack_summary,
    record_missed_message_ack_shutdown,
    run_missed_message_ack,
)


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
        self.window = (since, until)
        return list(self.records)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SimpleNamespace(success=True)


def runner(adapter, db, enabled=True):
    runner = SimpleNamespace(
        config=SimpleNamespace(missed_inbound_recovery={"enabled": enabled}),
        adapters={"slack": adapter}, _session_db=db, _background_tasks=set(),
    )
    return runner


@pytest.fixture(autouse=True)
def _enable_feature_for_unit_tests(monkeypatch):
    monkeypatch.setattr("gateway.missed_message_ack._test_mode", lambda: False)


def records(count=1, conversation="D1", start=1500):
    return [
        {"platform": "slack", "conversation": conversation, "ts": str(start + i),
         "thread_ts": str(start + i), "event": {"ts": str(start + i), "text": "missed"}}
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_missed_dm_ack_once_and_existing_dedupe():
    db = DB()
    adapter = Adapter(records(start=1999))
    gw = runner(adapter, db)
    await _ack_one(db, gw, records(start=1999)[0], "offline")
    await _ack_one(db, gw, records(start=1999)[0], "offline")
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == "offline"


@pytest.mark.asyncio
async def test_cap_posts_one_summary_per_conversation():
    db = DB()
    adapter = Adapter(records(25, conversation="C1"))
    gw = runner(adapter, db)
    for item in records(20, conversation="C1"):
        await _ack_one(db, gw, item, "offline")
    await _ack_summary(db, gw, "slack", "C1", records(5, conversation="C1", start=3000))
    assert len(adapter.sent) == 21
    assert "missed 5 messages" in adapter.sent[-1][1]


@pytest.mark.asyncio
async def test_errors_are_swallowed_and_scan_does_not_advance():
    class Broken(Adapter):
        async def fetch_missed_inbound(self, since, until):
            raise RuntimeError("mock API failure")

    db = DB()
    await run_missed_message_ack(runner(Broken([]), db), now=2000)
    assert ("scan", "slack") not in db.values


@pytest.mark.asyncio
async def test_disabled_does_not_fetch_or_dispatch():
    adapter = Adapter(records())
    await run_missed_message_ack(runner(adapter, DB(), enabled=False), now=2000)
    assert not adapter.sent


@pytest.mark.asyncio
async def test_shutdown_marker_defines_next_offline_window():
    db = DB()
    await record_missed_message_ack_shutdown(SimpleNamespace(_session_db=db), now=1900)
    adapter = Adapter(records(start=1999))
    await run_missed_message_ack(runner(adapter, db), now=2000)
    assert adapter.window == (1900.0, 2000)


def test_config_serializes_and_defaults():
    cfg = GatewayConfig.from_dict({"gateway": {"missed_inbound_recovery": {"enabled": False}}})
    assert cfg.missed_inbound_recovery_enabled is False
    assert cfg.to_dict()["missed_inbound_recovery"] == {"enabled": False}
