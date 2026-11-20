"""Messages missed while a platform was disconnected are answered late, exactly once.

Drives ``gateway.missed_inbound_recovery`` through its real boundary: a real ``SessionDB`` for
the durable watermark and claims, and a recording adapter in place of a platform.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from gateway import missed_inbound_recovery as recovery
from gateway.config import GatewayConfig, Platform
from hermes_state import AsyncSessionDB, SessionDB


class RecordingAdapter:
    """A platform that reports ``records`` as missed and records what recovery does with them."""

    is_connected = True
    # Set by recovery when it adopts the adapter.
    missed_inbound_observer: Any = None
    missed_inbound_reconnected: Any = None

    def __init__(self, records=(), *, fetch_error=None, dispatch_error=None):
        self.records = list(records)
        self.fetch_error, self.dispatch_error = fetch_error, dispatch_error
        self.windows, self.dispatched, self.sent, self.recovered = [], [], [], []
        self.live_until: Any = None
        self.missed_inbound_scan_complete = True

    async def fetch_missed_inbound(self, since, until):
        self.windows.append((since, until))
        if self.fetch_error:
            raise self.fetch_error
        return list(self.records)

    async def dispatch_missed_inbound(self, record, note):
        self.dispatched.append((record["conversation"], record["ts"], note))
        if self.dispatch_error:
            raise self.dispatch_error

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))

    def mark_missed_inbound_recovered(self, conversation, ts):
        self.recovered.append((conversation, ts))


class ProbedAdapter(RecordingAdapter):
    """Reports its transport state through ``missed_inbound_live_until``."""

    def missed_inbound_live_until(self):
        return self.live_until


def record(conversation, ts):
    return {"conversation": conversation, "ts": ts}


@pytest.fixture(autouse=True)
def no_settle_wait(monkeypatch):
    """A scheduled scan waits for the transport to settle; these adapters have none."""
    monkeypatch.setattr(recovery, "RECONNECT_SETTLE_SECONDS", 0)


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def make_runner(db, adapters, *, enabled: Any = True) -> Any:
    """A gateway as recovery sees it; ``tasks`` holds what it spawned."""
    tasks = []
    runner = SimpleNamespace(
        config=SimpleNamespace(missed_inbound_recovery_enabled=enabled), _session_db=db,
        adapters=dict(adapters), tasks=tasks)
    runner._retain_background_task = lambda task: tasks.append(task) or task
    return runner


async def finish_scans(runner):
    """Wait for the scans recovery started, then stop its heartbeat."""
    scans = [task for task in runner.tasks if task.get_name() != "missed-inbound-heartbeat"]
    await asyncio.gather(*scans)
    for task in runner.tasks:
        task.cancel()
    await asyncio.gather(*runner.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_missed_message_is_dispatched_once_with_the_late_note(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter([record("D1", "1500.000100")])

    assert await recovery.recover_platform(make_runner(db, {Platform.SLACK: adapter}), Platform.SLACK, until=2000.0) == 1

    [(conversation, ts, note)] = adapter.dispatched
    assert (conversation, ts) == ("D1", "1500.000100")
    assert note.startswith("[Delivered late: sent ")
    assert "the gateway was offline from " in note
    assert note.endswith(
        "Acknowledge the delay, answer what still applies, and confirm before acting on anything time-sensitive.]")
    assert adapter.sent == []
    assert adapter.recovered == [("D1", "1500.000100")]


@pytest.mark.asyncio
async def test_a_second_startup_does_not_dispatch_the_same_message_again(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    first = RecordingAdapter([record("D1", "1500.000100")])
    await recovery.recover_platform(make_runner(db, {Platform.SLACK: first}), Platform.SLACK, until=2000.0)

    # A crash loop: the next process sees the same history again, and the watermark may not have moved.
    db._execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value='1000.0' WHERE key='missed_inbound:watermark:slack'"))
    second = RecordingAdapter([record("D1", "1500.000100")])
    assert await recovery.recover_platform(make_runner(db, {Platform.SLACK: second}), Platform.SLACK, until=2100.0) == 0

    assert len(first.dispatched) == 1
    assert second.dispatched == []
    # The new process still learns what was recovered, so a platform replay is dropped live.
    assert ("D1", "1500.000100") in second.recovered


@pytest.mark.asyncio
async def test_claim_is_written_before_dispatch_and_survives_a_failed_dispatch(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter(
        [record("D1", "1500.000100"), record("D1", "1600.000100")], dispatch_error=RuntimeError("handler down"))

    assert await recovery.recover_platform(make_runner(db, {Platform.SLACK: adapter}), Platform.SLACK, until=2000.0) == 2

    # Both were attempted in order, and neither can be claimed again.
    assert [ts for _, ts, _ in adapter.dispatched] == ["1500.000100", "1600.000100"]
    assert db.claim_missed_inbound("slack", "D1", "1500.000100") is False
    assert db.claim_missed_inbound("slack", "D1", "1600.000100") is False


@pytest.mark.asyncio
async def test_messages_are_dispatched_oldest_first(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter([
        record("D1", "1700.000100"), record("C1", "1600.000100"), record("D1", "1500.000100"),
        record("D1", "1500.000100")])

    await recovery.recover_platform(make_runner(db, {Platform.SLACK: adapter}), Platform.SLACK, until=2000.0)

    assert [(c, ts) for c, ts, _ in adapter.dispatched] == [
        ("D1", "1500.000100"), ("C1", "1600.000100"), ("D1", "1700.000100")]


@pytest.mark.asyncio
async def test_overflow_past_the_cap_gets_one_summary_per_conversation(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    busy = [record("D1", f"{1100 + i}.000100") for i in range(recovery.MAX_DISPATCH_PER_SCAN + 3)]
    adapter = RecordingAdapter([*busy, record("C2", "1900.000100")])

    dispatched = await recovery.recover_platform(
        make_runner(db, {Platform.SLACK: adapter}), Platform.SLACK, until=2000.0)

    assert dispatched == recovery.MAX_DISPATCH_PER_SCAN == 20
    assert [ts for _, ts, _ in adapter.dispatched] == [item["ts"] for item in busy[:20]]
    assert [chat for chat, _ in adapter.sent] == ["D1", "C2"]
    assert "3 more messages" in adapter.sent[0][1]
    assert "1 more message " in adapter.sent[1][1]

    # Summarized messages are claimed too: the next startup neither dispatches nor summarizes them.
    db._execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value='1000.0' WHERE key='missed_inbound:watermark:slack'"))
    again = RecordingAdapter(adapter.records)
    assert await recovery.recover_platform(make_runner(db, {Platform.SLACK: again}), Platform.SLACK, until=2000.0) == 0
    assert again.dispatched == [] and again.sent == []


@pytest.mark.asyncio
async def test_messages_outside_the_offline_interval_are_ignored(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter([
        record("D1", "999.000100"), record("D1", "1000.000000"), record("D1", "1500.000100"),
        record("D1", "2000.000001"), record("D1", "not-a-ts"), record("", "1500.000200"), "junk"])

    await recovery.recover_platform(make_runner(db, {Platform.SLACK: adapter}), Platform.SLACK, until=2000.0)

    assert [ts for _, ts, _ in adapter.dispatched] == ["1500.000100"]


@pytest.mark.asyncio
async def test_the_previous_runs_uptime_is_not_rescanned(db):
    adapter = RecordingAdapter()
    runner = make_runner(db, {Platform.SLACK: adapter})

    # First connection ever: nothing was missed, so nothing is fetched.
    assert await recovery.recover_platform(runner, Platform.SLACK, until=1000.0) == 0
    assert adapter.windows == []

    # The gateway stays up until 5000, then is offline until 5600.
    await recovery.heartbeat_once(runner, now=5000.0)
    restarted = RecordingAdapter([record("D1", "3000.000100"), record("D1", "5300.000100")])
    await recovery.recover_platform(make_runner(db, {Platform.SLACK: restarted}), Platform.SLACK, until=5600.0)

    assert restarted.windows == [(5000.0, 5600.0)]
    # 3000 was sent while the previous run was up and connected: it is not a missed message.
    assert [ts for _, ts, _ in restarted.dispatched] == ["5300.000100"]
    assert db.get_missed_inbound_watermark("slack") == 5600.0


@pytest.mark.asyncio
async def test_the_window_never_reaches_back_further_than_a_day(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    until = 1000.0 + 3 * recovery.MAX_WINDOW_SECONDS
    adapter = RecordingAdapter()

    await recovery.recover_platform(make_runner(db, {Platform.SLACK: adapter}), Platform.SLACK, until=until)

    assert adapter.windows == [(until - 24 * 60 * 60, until)]


@pytest.mark.asyncio
async def test_a_message_handled_live_is_never_replayed_late(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter([record("D1", "1500.000100")])
    runner = make_runner(db, {Platform.SLACK: adapter})

    recovery.schedule_missed_inbound_recovery(runner, Platform.SLACK)
    await finish_scans(runner)
    adapter.dispatched.clear()
    adapter.missed_inbound_observer("D1", "1800.000100")  # what the adapter calls for a live message
    await asyncio.gather(*[t for t in runner.tasks if t.get_name() == "missed-inbound-live-claim"])

    adapter.records = [record("D1", "1800.000100")]
    db._execute_write(lambda conn: conn.execute(
        "UPDATE state_meta SET value='1000.0' WHERE key='missed_inbound:watermark:slack'"))
    assert await recovery.recover_platform(runner, Platform.SLACK, until=2000.0) == 0
    assert adapter.dispatched == []
    # A live claim is not something recovery answered, so it is not reported as recovered.
    assert ("D1", "1800.000100") not in db.list_recovered_missed_inbound("slack")


@pytest.mark.asyncio
async def test_reconnect_scans_only_the_platform_that_reconnected(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    db.advance_missed_inbound_watermark("telegram", 1000.0)
    slack, telegram = RecordingAdapter(), RecordingAdapter()
    runner = make_runner(db, {Platform.SLACK: slack, Platform.TELEGRAM: telegram})

    recovery.schedule_missed_inbound_recovery(runner, Platform.SLACK)
    await finish_scans(runner)

    assert len(slack.windows) == 1
    assert telegram.windows == []
    assert db.get_missed_inbound_watermark("telegram") == 1000.0


@pytest.mark.asyncio
async def test_publishing_an_adapter_schedules_recovery_for_that_platform_only(monkeypatch):
    from gateway.run import GatewayRunner

    scheduled = []
    monkeypatch.setattr(recovery, "schedule_missed_inbound_recovery", lambda runner, platform: scheduled.append(platform))
    runner = SimpleNamespace(
        adapters={Platform.TELEGRAM: object()}, _sync_voice_mode_state_to_adapter=lambda adapter: None,
        _bind_voice_input_callback=lambda adapter: None)

    GatewayRunner._publish_primary_adapter(runner, Platform.SLACK, object())

    assert scheduled == [Platform.SLACK]


@pytest.mark.asyncio
async def test_errors_are_swallowed_and_startup_is_not_blocked(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter(fetch_error=RuntimeError("slack is down"))
    runner = make_runner(db, {Platform.SLACK: adapter})

    recovery.schedule_missed_inbound_recovery(runner, Platform.SLACK)  # returns before any I/O
    assert adapter.windows == []
    await finish_scans(runner)
    assert len(adapter.windows) == 1
    assert adapter.dispatched == []

    # Nothing about a broken runner or adapter reaches the caller either.
    recovery.schedule_missed_inbound_recovery(object(), Platform.SLACK)
    recovery.schedule_missed_inbound_recovery(make_runner(None, {Platform.SLACK: adapter}), Platform.SLACK)
    assert await recovery.recover_platform(make_runner(db, {}), Platform.SLACK, until=2000.0) == 0


@pytest.mark.asyncio
async def test_a_partial_scan_keeps_the_watermark_for_retry(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter([record("D1", "1500.000100")])
    adapter.missed_inbound_scan_complete = False

    assert await recovery.recover_platform(make_runner(db, {Platform.SLACK: adapter}), Platform.SLACK, until=2000.0) == 1
    assert db.get_missed_inbound_watermark("slack") == 1000.0
    assert len(adapter.dispatched) == 1


@pytest.mark.asyncio
async def test_recovery_is_off_unless_the_config_flag_is_literally_true(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter([record("D1", "1500.000100")])

    for enabled in (False, None, "yes", object()):
        runner = make_runner(db, {Platform.SLACK: adapter}, enabled=enabled)
        recovery.schedule_missed_inbound_recovery(runner, Platform.SLACK)
        assert runner.tasks == []
        assert await recovery.recover_platform(runner, Platform.SLACK, until=2000.0) == 0
        await recovery.heartbeat_once(runner, now=3000.0)

    assert adapter.windows == [] and adapter.dispatched == []
    assert db.get_missed_inbound_watermark("slack") == 1000.0


@pytest.mark.asyncio
async def test_a_transport_seen_down_is_scanned_when_it_comes_back(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = ProbedAdapter([record("D1", "5500.000100")])
    runner = make_runner(db, {Platform.SLACK: adapter})

    adapter.live_until = 5000.0
    await recovery.heartbeat_once(runner, now=5010.0)
    assert db.get_missed_inbound_watermark("slack") == 5000.0  # proven connected at 5000, not at 5010

    adapter.live_until = False
    await recovery.heartbeat_once(runner, now=5400.0)
    adapter.live_until = None  # unknown: neither up nor down
    await recovery.heartbeat_once(runner, now=5500.0)
    assert db.get_missed_inbound_watermark("slack") == 5000.0
    assert adapter.windows == []

    adapter.live_until = 6000.0
    await recovery.heartbeat_once(runner, now=6000.0)
    await finish_scans(runner)

    assert adapter.windows == [(5000.0, 6000.0)]
    assert [ts for _, ts, _ in adapter.dispatched] == ["5500.000100"]
    assert db.get_missed_inbound_watermark("slack") == 6000.0


@pytest.mark.asyncio
async def test_the_heartbeat_does_not_move_the_watermark_under_a_running_scan(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    fetching, release = asyncio.Event(), asyncio.Event()

    class SlowAdapter(RecordingAdapter):
        async def fetch_missed_inbound(self, since, until):
            self.windows.append((since, until))
            fetching.set()
            await release.wait()
            return []

    adapter = SlowAdapter()
    runner = make_runner(db, {Platform.SLACK: adapter})
    scan = asyncio.ensure_future(recovery.recover_platform(runner, Platform.SLACK, until=2000.0))
    await fetching.wait()
    await recovery.heartbeat_once(runner, now=9000.0)
    assert db.get_missed_inbound_watermark("slack") == 1000.0

    release.set()
    await scan
    assert db.get_missed_inbound_watermark("slack") == 2000.0


@pytest.mark.asyncio
async def test_a_transport_reconnect_scans_once_after_it_settles(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter()
    runner = make_runner(db, {Platform.SLACK: adapter})
    recovery.schedule_missed_inbound_recovery(runner, Platform.SLACK)
    await finish_scans(runner)
    adapter.windows.clear()
    runner.tasks.clear()

    adapter.missed_inbound_reconnected()  # what the adapter calls when it rebuilds its transport
    adapter.missed_inbound_reconnected()  # a flapping socket does not stack scans
    await finish_scans(runner)

    assert len(adapter.windows) == 1


@pytest.mark.asyncio
async def test_recovery_works_through_the_gateways_async_database_door(db):
    db.advance_missed_inbound_watermark("slack", 1000.0)
    adapter = RecordingAdapter([record("D1", "1500.000100")])

    runner = make_runner(AsyncSessionDB(db), {Platform.SLACK: adapter})
    assert await recovery.recover_platform(runner, Platform.SLACK, until=2000.0) == 1


def test_the_watermark_only_moves_forward_and_old_claims_are_pruned(db):
    import time

    assert db.get_missed_inbound_watermark("slack") is None
    db.advance_missed_inbound_watermark("slack", 2000.0)
    db.advance_missed_inbound_watermark("slack", 1500.0)
    assert db.get_missed_inbound_watermark("slack") == 2000.0

    assert db.claim_missed_inbound("slack", "D1", "1500.000100") is True
    assert db.claim_missed_inbound("slack", "D1", "1500.000100") is False
    assert db.claim_missed_inbound("slack", "D2", "1500.000100") is True
    assert db.claim_missed_inbound("telegram", "D1", "1500.000100") is True
    assert db.claim_missed_inbound("slack", "D1", "1600.000100", live=True) is True
    assert sorted(db.list_recovered_missed_inbound("slack")) == [("D1", "1500.000100"), ("D2", "1500.000100")]

    assert db.prune_missed_inbound_claims(time.time() - 3600) == 0
    assert db.prune_missed_inbound_claims(time.time() + 3600) == 4
    assert db.list_recovered_missed_inbound("slack") == []
    assert db.get_missed_inbound_watermark("slack") == 2000.0


def test_recovery_defaults_off_in_tests_and_on_for_a_real_gateway(monkeypatch):
    assert GatewayConfig().missed_inbound_recovery_enabled is False
    assert GatewayConfig.from_dict({}).missed_inbound_recovery_enabled is False

    monkeypatch.setattr("gateway.config._in_test_context", lambda: False)
    assert GatewayConfig().missed_inbound_recovery_enabled is True
    assert GatewayConfig.from_dict({}).missed_inbound_recovery_enabled is True
    off = GatewayConfig.from_dict({"missed_inbound_recovery": {"enabled": False}})
    assert off.missed_inbound_recovery_enabled is False
    assert GatewayConfig.from_dict(off.to_dict()).missed_inbound_recovery_enabled is False


def test_recovery_can_be_enabled_from_config(monkeypatch):
    assert GatewayConfig.from_dict({"missed_inbound_recovery": {"enabled": True}}).missed_inbound_recovery_enabled is True
    nested = GatewayConfig.from_dict({"gateway": {"missed_inbound_recovery": {"enabled": True}}})
    assert nested.missed_inbound_recovery_enabled is True
    assert nested.to_dict()["missed_inbound_recovery"] == {"enabled": True}


def test_gateway_config_yaml_reaches_the_flag(monkeypatch, tmp_path):
    from gateway.config import load_gateway_config

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("gateway:\n  missed_inbound_recovery:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert load_gateway_config().missed_inbound_recovery_enabled is True
