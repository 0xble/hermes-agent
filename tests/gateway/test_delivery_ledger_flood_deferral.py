"""Flood-control rejections are deferred, not terminally failed.

A ``flood_control:<wait>`` rejection is the most retryable class the ledger
sees — the server states exactly when to come back — but it could never match
the exact-string runtime allowlist, so the row stayed ``failed`` with
``attempts=0`` and the completed answer was destroyed. Observed in production
as three lost answers (12527 / 5464 / 694 chars) behind a 67-minute Telegram
penalty.

Nothing re-drives such a row in a live process either: the reconnect sweep
needs a reconnect (a flood ban disconnects nothing) and startup recovery
deliberately ignores live-owner rows. Hence the deferred sweep.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway import delivery_ledger as dl


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


def _record(oid="ob-1", platform="telegram"):
    dl.record_obligation(
        obligation_id=oid,
        session_key="agent:main:telegram:dm:123:456",
        platform=platform,
        chat_id="123",
        thread_id="456",
        content="the final answer",
    )
    dl.mark_attempting(oid)


def _row(oid):
    with dl._connect() as conn:
        cur = conn.execute(
            "SELECT state, attempts, last_error FROM delivery_obligations "
            "WHERE obligation_id=?",
            (oid,),
        )
        state, attempts, last_error = cur.fetchone()
    return {"state": state, "attempts": attempts, "last_error": last_error}


class TestFloodErrorClassification:
    @pytest.mark.parametrize(
        "error,expected",
        [
            ("flood_control:3459", 3459.0),
            ("flood_control:0", 0.0),
            ("flood_control:12.5", 12.5),
            ("FLOOD_CONTROL:60", 60.0),
            ("flood_control:-5", 0.0),          # clamped, never negative
            ("flood_control:bogus", None),
            ("flood_control:", None),
            ("flood_control:inf", None),
            ("flood_control:nan", None),
            ("send_path_degraded", None),
            ("Message thread not found", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse_flood_retry_after(self, error, expected):
        assert dl.parse_flood_retry_after(error) == expected

    def test_flood_errors_are_runtime_retryable(self):
        assert dl.is_runtime_retryable("flood_control:3459") is True

    def test_existing_allowlist_still_retryable(self):
        assert dl.is_runtime_retryable("send_path_degraded") is True

    def test_genuinely_terminal_errors_are_not_retryable(self):
        assert dl.is_runtime_retryable("Message thread not found") is False
        assert dl.is_runtime_retryable("chat not found") is False


class TestFloodRowsAreClaimable:
    def test_flood_failed_row_is_claimed_by_the_live_process(self):
        """The regression: this row used to be skipped forever."""
        _record()
        dl.mark_failed("ob-1", "flood_control:3459")

        claimed = dl.sweep_failed_for_runtime("telegram")

        assert [r["obligation_id"] for r in claimed] == ["ob-1"]
        assert _row("ob-1")["state"] == "attempting"
        assert _row("ob-1")["attempts"] == 1

    def test_claimed_row_carries_its_failure_class(self):
        """So a release can restore it instead of flattening to
        send_path_degraded, which would re-send inside the penalty window."""
        _record()
        dl.mark_failed("ob-1", "flood_control:3459")

        claimed = dl.sweep_failed_for_runtime("telegram")

        assert claimed[0]["last_error"] == "flood_control:3459"

    def test_terminal_failure_is_still_left_alone(self):
        _record()
        dl.mark_failed("ob-1", "Message thread not found")

        assert dl.sweep_failed_for_runtime("telegram") == []
        assert _row("ob-1")["state"] == "failed"

    def test_attempts_cap_still_bounds_flood_rows(self):
        """Deferral must not create a poison row that retries forever."""
        _record()
        dl.mark_failed("ob-1", "flood_control:3459")
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET attempts=? WHERE obligation_id=?",
                (dl.MAX_ATTEMPTS, "ob-1"),
            )

        assert dl.sweep_failed_for_runtime("telegram") == []
        assert _row("ob-1")["state"] == "abandoned"


class TestDeferredSweepScheduling:
    """The scheduler is exercised through the real GatewayRunner method bound
    onto a minimal stub — constructing a full runner would drag in the whole
    gateway for what is a small timer contract."""

    def _stub(self, calls):
        from gateway.run import GatewayRunner

        stub = SimpleNamespace(_deferred_obligation_sweeps={})

        async def _redeliver(platform, *, profile=None):
            calls.append((platform, profile))
            return 1

        stub._redeliver_failed_obligations_for_platform = _redeliver
        stub._schedule_deferred_obligation_redelivery = (
            GatewayRunner._schedule_deferred_obligation_redelivery.__get__(stub)
        )
        return stub

    @pytest.mark.asyncio
    async def test_sweep_runs_after_the_wait_elapses(self):
        calls = []
        stub = self._stub(calls)

        stub._schedule_deferred_obligation_redelivery(
            "telegram", profile=None, delay=0.0,
        )
        assert calls == []                      # not immediate
        await asyncio.sleep(1.3)                # delay + the 1s boundary margin
        assert calls == [("telegram", None)]

    @pytest.mark.asyncio
    async def test_burst_inside_one_window_coalesces_to_one_sweep(self):
        """A penalty rejects every queued answer; that must not schedule a
        timer per lost answer."""
        calls = []
        stub = self._stub(calls)

        for _ in range(25):
            stub._schedule_deferred_obligation_redelivery(
                "telegram", profile=None, delay=0.0,
            )

        assert len(stub._deferred_obligation_sweeps) == 1
        await asyncio.sleep(1.3)
        assert calls == [("telegram", None)]

    @pytest.mark.asyncio
    async def test_distinct_identities_get_distinct_timers(self):
        calls = []
        stub = self._stub(calls)

        stub._schedule_deferred_obligation_redelivery(
            "telegram", profile=None, delay=30.0,
        )
        stub._schedule_deferred_obligation_redelivery(
            "telegram", profile="second-bot", delay=30.0,
        )
        stub._schedule_deferred_obligation_redelivery(
            "slack", profile=None, delay=30.0,
        )

        assert len(stub._deferred_obligation_sweeps) == 3
        for task in stub._deferred_obligation_sweeps.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_an_earlier_wait_supersedes_a_later_pending_one(self):
        calls = []
        stub = self._stub(calls)

        stub._schedule_deferred_obligation_redelivery(
            "telegram", profile=None, delay=3000.0,
        )
        first = stub._deferred_obligation_sweeps["telegram:default"]
        stub._schedule_deferred_obligation_redelivery(
            "telegram", profile=None, delay=0.0,
        )

        # cancel() only takes effect on the next loop turn.
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()
        await asyncio.sleep(1.3)
        assert calls == [("telegram", None)]

    @pytest.mark.asyncio
    async def test_delay_is_bounded_by_the_ledger_stale_cutoff(self):
        """A garbled or hostile retry_after must not park an answer forever."""
        calls = []
        stub = self._stub(calls)

        stub._schedule_deferred_obligation_redelivery(
            "telegram", profile=None, delay=10**9,
        )

        task = stub._deferred_obligation_sweeps["telegram:default"]
        assert task._hermes_sweep_at <= time.monotonic() + dl.STALE_AFTER_SECONDS + 1
        task.cancel()
