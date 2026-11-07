"""Private receipt evidence survives log churn and never owns delivery."""
import asyncio
import json
import threading
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import reply_latency
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.config import Platform


def incoming(message_id: str | None = "one", profile="default", **kwargs):
    return MessageEvent(text="private question", message_id=message_id,
                        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat",
                                             thread_id="thread", profile=profile), **kwargs)


def rows(home):
    from gateway.reply_latency_sink import sink_for_home
    sink_for_home(home).flush()
    return [json.loads(line) for path in sorted((home / "logs/reply-latency").glob("*.jsonl"))
            for line in path.read_text().splitlines()]


def test_private_daily_sink_survives_log_rotation_restart_and_retention(tmp_path, monkeypatch):
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
    # Real process restarts reuse the directory, not a module's singleton/UUID.
    for _ in range(2):
        subprocess.run([sys.executable, '-c',
                        'import sys; from pathlib import Path; '
                        'from gateway.reply_latency_sink import checkpoint_profiles; '
                        'checkpoint_profiles({"default": Path(sys.argv[1])}); '
                        'from gateway.reply_latency_sink import sink_for_home; '
                        'sink_for_home(Path(sys.argv[1])).flush()', str(home)],
                       check=True, cwd=Path(__file__).resolve().parents[2],
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600
                   for path in root.glob("*.jsonl") if path != recent)
    starts = [row for row in rows(home) if row.get("event") == "instrumentation_start"]
    assert len({row["process_id"] for row in starts}) == 3
    assert all("started_at" in row for row in starts)


@pytest.mark.asyncio
async def test_idle_profile_start_checkpoints_and_failed_interval(tmp_path, monkeypatch):
    from gateway import reply_latency_sink as sink
    home = tmp_path / 'named'
    runner = SimpleNamespace(_running=True, _served_profile_homes={'named': home})
    async def stop_after_first_tick(seconds):
        runner._running = False
    monkeypatch.setattr(sink.asyncio, 'sleep', stop_after_first_tick)
    await sink.coverage_forever(runner)
    assert [row['event'] for row in rows(home)] == ['instrumentation_start', 'coverage']
    assert all(row['profile'] == 'named' for row in rows(home))
    assert rows(home)[1]['coverage_start'] >= rows(home)[0]['timestamp']
    instance = sink.sink_for_home(home, 'named')
    append = instance._append
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(instance, '_append', fail)
    instance.checkpoint()
    instance.flush()
    assert instance.coverage_start is None
    monkeypatch.setattr(instance, '_append', append)
    instance.checkpoint()
    last = rows(home)[-1]
    assert last['coverage_start'] == last['timestamp']


@pytest.mark.asyncio
async def test_slow_writer_never_blocks_delivery_loop_and_overflow_breaks_coverage(tmp_path, monkeypatch):
    from gateway.reply_latency_sink import append_event, sink_for_home
    instance = sink_for_home(tmp_path)
    with monkeypatch.context() as hot_path:
        def filesystem_lookup(*args, **kwargs):
            raise AssertionError("sink hot path must not resolve/stat the home")
        hot_path.setattr(Path, 'resolve', filesystem_lookup)
        append_event(tmp_path, {'profile': 'default', 'event': 'agent_start', 'timestamp': 1})
    instance.flush()
    entered, release = threading.Event(), threading.Event()
    original = instance._append
    def slow(*args):
        entered.set()
        assert release.wait(5)
        original(*args)
    monkeypatch.setattr(instance, '_append', slow)
    instance.checkpoint()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # The disk thread is blocked, yet this loop can enqueue and keep scheduling.
        for _ in range(instance.pending.maxsize + 1):
            instance.checkpoint()
        await asyncio.sleep(0)
        assert instance.interrupted.is_set()
    finally:
        release.set()
    await asyncio.to_thread(instance.flush)
    instance.checkpoint()
    await asyncio.to_thread(instance.flush)
    recorded = [json.loads(line) for path in instance.root.glob('*.jsonl')
                for line in path.read_text().splitlines()]
    checkpoints = [row for row in recorded if row['event'] == 'coverage']
    assert any(row['coverage_start'] == row['timestamp'] for row in checkpoints[1:])


def test_alias_homes_share_writer_and_sequence(tmp_path):
    from gateway.reply_latency_sink import sink_for_home
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    first = sink_for_home(real, 'default')
    second = sink_for_home(alias, 'named')
    assert first is second
    first.write({'event': 'received', 'profile': 'default', 'timestamp': 1})
    second.write({'event': 'first_reply', 'profile': 'named', 'timestamp': 2})
    second.flush()
    records = rows(real)
    assert len({row['process_id'] for row in records}) == 1
    assert [row['sequence'] for row in records] == list(range(1, len(records) + 1))


def test_shutdown_drains_queue_stops_threads_and_reaps_registry(tmp_path):
    from gateway import reply_latency_sink as sinks
    instance = sinks.sink_for_home(tmp_path)
    for index in range(8):
        instance.write({'event': 'received', 'profile': 'default', 'timestamp': index})
    assert sinks.shutdown_sinks(timeout=2)
    assert not instance.worker.is_alive()
    assert not sinks._sinks
    records = [json.loads(line) for p in instance.root.glob('*.jsonl') for line in p.read_text().splitlines()]
    assert len([row for row in records if row['event'] == 'received']) == 8


def test_shutdown_slow_disk_wait_is_bounded_and_can_be_reaped_later(tmp_path, monkeypatch):
    from gateway import reply_latency_sink as sinks
    instance = sinks.sink_for_home(tmp_path)
    entered, release = threading.Event(), threading.Event()
    append = instance._append
    def slow(*args):
        entered.set()
        assert release.wait(5)
        append(*args)
    monkeypatch.setattr(instance, '_append', slow)
    instance.checkpoint()
    try:
        assert entered.wait(2)
        assert not sinks.shutdown_sinks(timeout=0)
        assert instance.closed.is_set()
        assert instance.worker.is_alive()
        assert sinks.sink_for_home(tmp_path) is instance
    finally:
        release.set()
    assert sinks.shutdown_sinks(timeout=2)
    assert not instance.worker.is_alive()


def test_pre_initialization_overflow_cannot_be_healed_by_old_queued_event(tmp_path):
    from gateway.reply_latency_sink import ReplyLatencySink
    instance = ReplyLatencySink(tmp_path)
    # Deterministically model the writer after detecting overflow before its first append.
    instance.coverage_start = None
    instance.unproven_until = 100001
    instance._write({'event': 'received', 'profile': 'default', 'timestamp': 100000}, 100000)
    assert instance.coverage_start is None
    instance._write(None, 100005)
    records = [json.loads(line) for path in instance.root.glob('*.jsonl')
               for line in path.read_text().splitlines()]
    assert records[-1]['event'] == 'coverage'
    assert records[-1]['coverage_start'] == records[-1]['timestamp'] == 100005


def test_idless_receipt_survives_native_envelope_restore(tmp_path, monkeypatch):
    from gateway.session_envelope import _adapter_event, _snapshot_native, restore_native
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = incoming(None)
    first = reply_latency.record_received(original)
    assert first is not None
    projected = _adapter_event(original, text="private question")
    runner = SimpleNamespace(session_store=SimpleNamespace(_generate_session_key=lambda source: "route"))
    payload = _snapshot_native(runner, projected, None,
                               encoded_source={**original.source.to_dict(), "is_bot": False})
    restored = restore_native(payload)
    second = reply_latency.record_received(restored)
    assert second is not None
    assert first.identity['receipt_id'] == second.identity['receipt_id']
    assert first.identity['message_id'] is second.identity['message_id'] is None
    assert restored._attended_activity_received_at == original._attended_activity_received_at
    assert reply_latency.record_received(incoming(None)).identity['receipt_id'] != first.identity['receipt_id']


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
    from gateway.reply_latency_sink import sink_for_home
    sink_for_home(home).flush()
    assert sum("reply-latency sink unavailable" in r.message for r in caplog.records) == 1
    for origin, metadata in {
        "goal": {"goal_continuation": True}, "loop": {"loop_wakeup": True},
        "cron": {"notification_origin": "cron_delivery"},
        "relay": {"notification_origin": "relay_delivery"},
        "process": {"notification_origin": "process_registry_synthetic"},
    }.items():
        synthetic = incoming(origin, metadata=metadata)
        assert reply_latency.turn_origin_for_event(synthetic) != "user"
        receipt = reply_latency.record_received(synthetic)
        assert receipt.identity['origin'] != 'user'
        reply_latency.bind_agent_start(synthetic)
        assert reply_latency.current_receipt() is receipt
        assert reply_latency.current_receipt() is not human._reply_latency_receipt
