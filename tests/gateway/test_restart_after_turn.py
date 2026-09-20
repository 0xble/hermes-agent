"""Unit tests for in-band restart after-turn deferral helpers (#77184)."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
    parse_restart_after_turn_timeout,
    resolve_restart_exit_wait_budget,
)
from gateway.run import GatewayRunner


def test_parse_restart_after_turn_timeout_defaults_and_clamps():
    assert parse_restart_after_turn_timeout("") == DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    assert parse_restart_after_turn_timeout(None) == DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    assert parse_restart_after_turn_timeout("bogus") == DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    assert parse_restart_after_turn_timeout(0) == 0.0
    assert parse_restart_after_turn_timeout("-5") == 0.0
    assert parse_restart_after_turn_timeout("120") == 120.0


def test_default_restart_after_turn_timeout_is_human_tolerable():
    """The shipped default must not make interactive restarts block for hours.

    A wedged turn must not pin `hermes gateway restart` for 6h — the
    default is a safety valve for hung agents, not a target latency
    (#79133). 900-1800s protects long autonomous turns while keeping
    worst-case interactive restart in human-tolerable territory.
    """
    assert 900 <= DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT <= 1800
    # An interactive restart's printed wait budget stays under ~32 min.
    budget = resolve_restart_exit_wait_budget(
        60, DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT, headroom=15
    )
    assert budget <= 1875


def test_resolve_restart_exit_wait_budget_covers_both_phases():
    assert resolve_restart_exit_wait_budget(0, 0, headroom=15) == 15.0
    assert resolve_restart_exit_wait_budget(180, 21600, headroom=15) == 180 + 21600 + 15
    assert resolve_restart_exit_wait_budget(180, 60, delegation_timeout=21600, headroom=15) == 180 + 21600 + 15
    assert resolve_restart_exit_wait_budget(180, 21600, delegation_timeout=0, headroom=15) == 180 + 21600 + 15
    assert resolve_restart_exit_wait_budget("bad", "bad", headroom="x") == 0.0


def test_load_restart_after_turn_timeout_preserves_zero(tmp_path, monkeypatch):
    """Config/env ``0`` must disable after-turn wait, not fall back to default."""
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_RESTART_AFTER_TURN_TIMEOUT", raising=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "agent:\n  restart_after_turn_timeout: 0\n",
        encoding="utf-8",
    )
    assert GatewayRunner._load_restart_after_turn_timeout() == 0.0

    monkeypatch.setenv("HERMES_RESTART_AFTER_TURN_TIMEOUT", "0")
    assert GatewayRunner._load_restart_after_turn_timeout() == 0.0


async def _clear_live_delegation(live, delay):
    await asyncio.sleep(delay)
    live.clear()


def test_restart_wait_includes_live_delegation_past_chat_cap(monkeypatch):
    async def scenario():
        from tools import async_delegation
        from tests.gateway.restart_test_helpers import make_restart_runner

        runner, _ = make_restart_runner()
        runner._restart_after_turn_timeout = 0.03
        runner._restart_delegation_timeout = 0.3
        live = [{"delegation_id": "deleg_x", "status": "running", "dispatched_at": time.time()}]
        monkeypatch.setattr(async_delegation, "active_count", lambda: len(live))
        monkeypatch.setattr(async_delegation, "active_records", lambda: list(live), raising=False)
        clear_task = asyncio.create_task(_clear_live_delegation(live, 0.1))
        assert any(unit["kind"] == "delegation" for unit in runner._describe_active_work())
        started = time.monotonic()
        try:
            assert await runner._await_active_work_before_restart() is True
        finally:
            await clear_task
        assert time.monotonic() - started >= 0.08
        assert not live

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "turn_timeout,delegation_timeout,turn_finishes_at,delegation_finishes_at,expected_wait",
    [
        (0.03, 0.3, None, 0.1, 0.1),
        (0.3, 0.03, 0.1, None, 0.1),
        (0, 0.3, None, 0.1, 0.1),
        (0, 0.3, None, None, 0.3),
    ],
)
def test_restart_wait_honors_independent_budgets(
    monkeypatch, caplog, turn_timeout, delegation_timeout,
    turn_finishes_at, delegation_finishes_at, expected_wait,
):
    """A stuck kind cannot borrow the other kind's budget, even with a zero cap."""
    import gateway.run_shutdown as shutdown
    from tools import async_delegation
    from tests.gateway.restart_test_helpers import make_restart_runner

    async def scenario():
        runner, _ = make_restart_runner()
        runner._restart_after_turn_timeout = turn_timeout
        runner._restart_delegation_timeout = delegation_timeout
        runner._running_agents["stuck-chat"] = object()
        live = [{"delegation_id": "deleg_x", "status": "running"}]
        monkeypatch.setattr(async_delegation, "active_count", lambda: len(live))
        monkeypatch.setattr(async_delegation, "active_records", lambda: list(live))
        clock = SimpleNamespace(now=0.0)

        async def tick(delay):
            clock.now += delay
            if turn_finishes_at is not None and clock.now >= turn_finishes_at:
                runner._running_agents.clear()
            if delegation_finishes_at is not None and clock.now >= delegation_finishes_at:
                live.clear()

        monkeypatch.setattr(shutdown, "asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now), sleep=tick,
        ))
        assert await runner._await_active_work_before_restart() is False
        assert clock.now == pytest.approx(expected_wait)
        assert runner._active_work_count() > 0
        # Each expired, nonzero kind is reported once, never once per poll.
        expected_expired = [
            kind for kind, timeout, finishes_at in (
                ("turn", turn_timeout, turn_finishes_at),
                ("delegation", delegation_timeout, delegation_finishes_at),
            ) if timeout > 0 and (finishes_at is None or finishes_at > timeout)
        ]
        for kind in expected_expired:
            assert sum(f"Restart {kind} wait timed out" in text for text in caplog.messages) == 1

    asyncio.run(scenario())


def test_restart_wait_status_reports_only_remaining_budgets(monkeypatch, caplog):
    import gateway.run_shutdown as shutdown
    from tools import async_delegation
    from tests.gateway.restart_test_helpers import make_restart_runner

    async def scenario():
        runner, _ = make_restart_runner()
        runner._restart_after_turn_timeout = 5
        runner._restart_delegation_timeout = 25
        runner._running_agents["stuck-chat"] = object()
        monkeypatch.setattr(async_delegation, "active_count", lambda: 1)
        monkeypatch.setattr(async_delegation, "active_records", lambda: [])
        clock = SimpleNamespace(now=0.0)

        async def tick(_delay):
            clock.now += 10

        monkeypatch.setattr(shutdown, "asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now), sleep=tick,
        ))
        caplog.set_level("INFO", logger="gateway.run")
        assert await runner._await_active_work_before_restart() is False
        statuses = [text for text in caplog.messages if text.startswith("Restart deferred:")]
        assert len(statuses) == 3
        assert "turns 5s left, delegations 25s left" in statuses[0]
        assert "delegations 15s left" in statuses[1] and "turns" not in statuses[1]
        assert "delegations 5s left" in statuses[2] and "turns" not in statuses[2]
        for kind in ("turn", "delegation"):
            assert sum(f"Restart {kind} wait timed out" in text for text in caplog.messages) == 1

    asyncio.run(scenario())


def test_restart_delegation_timeout_zero_does_not_wait(monkeypatch):
    async def scenario():
        from tools import async_delegation
        from tests.gateway.restart_test_helpers import make_restart_runner

        runner, _ = make_restart_runner()
        runner._restart_after_turn_timeout = 0.3
        runner._restart_delegation_timeout = 0
        live = [{"delegation_id": "deleg_x", "status": "running", "dispatched_at": time.time()}]
        monkeypatch.setattr(async_delegation, "active_count", lambda: len(live))
        monkeypatch.setattr(async_delegation, "active_records", lambda: list(live), raising=False)
        started = time.monotonic()
        assert await runner._await_active_work_before_restart() is False
        assert time.monotonic() - started < 0.08
        assert live

    asyncio.run(scenario())
