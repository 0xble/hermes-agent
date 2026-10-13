"""Restart-loop breaker behavior for planned and unplanned predecessor exits."""

from unittest.mock import Mock

from gateway import restart_loop_guard as guard
from gateway.run_startup import GatewayStartupMixin


def test_planned_restart_does_not_spend_boot_budget(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    assert guard.check_and_record(max_restarts=3, now=100.0) is False
    assert guard.check_and_record(max_restarts=3, now=110.0, planned_restart=True) is False
    assert guard._load_boots() == [100.0]


def test_repeated_planned_restart_loop_still_trips(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    assert guard.check_and_record(max_restarts=3, now=100.0) is False
    assert guard.check_and_record(max_restarts=3, now=110.0, planned_restart=True) is False
    assert guard.check_and_record(max_restarts=3, now=120.0, planned_restart=True) is False
    assert guard.check_and_record(max_restarts=3, now=130.0, planned_restart=True) is True


def test_startup_marks_pending_planned_restart_for_breaker(tmp_path, monkeypatch):
    class _Store:
        _entries = {}

        def _ensure_loaded_locked(self):
            return None

        class _Lock:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        _lock = _Lock()

    runner = object.__new__(GatewayStartupMixin)
    runner.session_store = _Store()
    monkeypatch.setattr("gateway.run._restart_notification_pending", lambda: True)
    monkeypatch.setattr("gateway.run._planned_restart_notification_pending", lambda: False)
    guard_call = Mock(return_value=False)
    monkeypatch.setattr(guard, "check_and_record", guard_call)

    assert runner._resume_pending_candidates() == []
    guard_call.assert_not_called()

    class _Entry:
        resume_pending = True
        suspended = False
        origin = object()
        resume_reason = "gateway_restart"

    _Store._entries = {"session": _Entry()}
    runner._AUTO_RESUME_REASONS = {"gateway_restart"}
    runner._restart_loop_guard_config = lambda: (3, 60, 300)
    assert runner._resume_pending_candidates() == [_Store._entries["session"]]
    guard_call.assert_called_once_with(3, 60, max_gap_seconds=300, planned_restart=True)
