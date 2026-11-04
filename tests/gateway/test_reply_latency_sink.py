"""Private receipt evidence survives log churn and never owns delivery."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import reply_latency
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.config import Platform


def incoming(message_id="one", profile="default", **kwargs):
    return MessageEvent(text="private question", message_id=message_id,
                        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat",
                                             thread_id="thread", profile=profile), **kwargs)


def rows(home):
    return [json.loads(line) for path in sorted((home / "logs/reply-latency").glob("*.jsonl"))
            for line in path.read_text().splitlines()]


def test_private_daily_sink_survives_log_rotation_restart_and_retention(tmp_path, monkeypatch):
    from gateway.reply_latency_sink import ReplyLatencySink

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    today = datetime.now(timezone.utc).date()
    root = home / "logs/reply-latency"
    root.mkdir(parents=True)
    old = root / f"{today - timedelta(days=14)}.jsonl"
    old.write_text("old\n")
    recent = root / f"{today - timedelta(days=13)}.jsonl"
    recent.write_text("{}\n")
    receipt = reply_latency.record_received(incoming())
    assert receipt is not None
    log = home / "logs/gateway.log"
    log.write_text("noisy\n")
    for _ in range(5):
        log.replace(log.with_suffix(".log.1"))
        log.write_text("rotated\n")
    reply_latency.record_first_reply(receipt, "private answer", SimpleNamespace(success=True))
    recorded = [row for row in rows(home) if row.get("event") in ("received", "first_reply")]
    assert [row["event"] for row in recorded] == ["received", "first_reply"]
    assert all(row["inbound_identity"] == ["default", "telegram", "chat", "thread", "one"]
               for row in recorded)
    assert all("private" not in json.dumps(row) for row in recorded)
    assert not old.exists() and recent.exists()
    # Independent instances model separate process lifetimes using the same private home.
    first, restarted = ReplyLatencySink(home), ReplyLatencySink(home)
    first.checkpoint()
    restarted.checkpoint()
    starts = [row for row in rows(home) if row.get("event") == "instrumentation_start"]
    assert len({row["process_id"] for row in starts}) == 3
    assert all("started_at" in row for row in starts)


@pytest.mark.asyncio
async def test_write_failure_does_not_break_confirmed_reply_and_origins_stay_non_user(tmp_path, monkeypatch, caplog):
    from gateway.reply_latency_sink import checkpoint_profiles
    from tests.gateway.test_reply_latency_logs import Adapter, event
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "logs").write_text("not a directory")
    adapter = Adapter()
    human = event(adapter, "human")
    reply_latency.bind_agent_start(human)
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig(cursor=""))
    assert await consumer._send_or_edit("answer", finalize=True)
    assert human._reply_latency_receipt.replied
    checkpoint_profiles({"default": home})
    assert sum("reply-latency sink unavailable" in r.message for r in caplog.records) == 1
    for origin, metadata in {
        "goal": {"goal_continuation": True}, "loop": {"loop_wakeup": True},
        "cron": {"notification_origin": "cron_delivery"},
        "relay": {"notification_origin": "relay_delivery"},
        "process": {"notification_origin": "process_registry_synthetic"},
    }.items():
        synthetic = incoming(origin, metadata=metadata)
        assert reply_latency.turn_origin_for_event(synthetic) != "user"
        assert reply_latency.record_received(synthetic) is None
        reply_latency.bind_agent_start(synthetic)
        assert reply_latency.current_receipt() is None
