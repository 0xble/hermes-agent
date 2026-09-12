"""Unit tests for in-band restart after-turn deferral helpers (#77184)."""

import pytest

from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
    parse_restart_after_turn_timeout,
    resolve_restart_exit_wait_budget,
    resolve_systemd_timeout_stop_sec,
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
    assert budget <= 1920


@pytest.mark.parametrize("drain, after_turn, cron", [(5, 30, 30), (5, 30, 120), (180, 30, 0), (0, 0, 0)])
def test_restart_observer_covers_configured_stop_and_startup(monkeypatch, drain, after_turn, cron):
    from hermes_cli.gateway import _get_restart_exit_wait_budget
    from hermes_constants import get_hermes_home

    for key in ("HERMES_RESTART_DRAIN_TIMEOUT", "HERMES_RESTART_AFTER_TURN_TIMEOUT", "HERMES_CRON_DRAIN_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    (get_hermes_home() / "config.yaml").write_text(
        f"agent:\n  restart_drain_timeout: {drain}\n  restart_after_turn_timeout: {after_turn}\n"
        f"  cron_drain_timeout: {cron}\n", encoding="utf-8",
    )
    stop_budget = resolve_systemd_timeout_stop_sec(drain, cron)
    budget = _get_restart_exit_wait_budget()
    assert budget == resolve_restart_exit_wait_budget(drain, after_turn, cron_drain_timeout=cron)
    assert budget > after_turn + stop_budget
    # The supported env bridge must preserve the explicit cron opt-out too.
    monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", "0")
    assert _get_restart_exit_wait_budget() == resolve_restart_exit_wait_budget(
        drain, after_turn, cron_drain_timeout=0,
    )


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
