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


def test_alternating_planned_and_crash_boots_trip_within_one_chain(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    # Each kind is 400s apart, but every boot is 200s apart. The merged chain
    # must count the second planned restart instead of treating it as a new exemption.
    for now, planned_restart in ((0.0, True), (200.0, False), (400.0, True), (600.0, False)):
        tripped = guard.check_and_record(
            max_restarts=3,
            now=now,
            max_gap_seconds=300,
            planned_restart=planned_restart,
        )

    assert tripped is True


def test_tripped_chain_resumes_once_for_planned_restart_then_trips(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)

    assert [guard.check_and_record(max_restarts=3, now=now) for now in (100.0, 110.0, 120.0)] == [
        False, False, True,
    ]
    assert guard.check_and_record(max_restarts=3, now=130.0, planned_restart=True) is False
    assert guard.check_and_record(max_restarts=3, now=140.0, planned_restart=True) is True


def test_legacy_boots_only_state_is_still_counted(tmp_path, monkeypatch):
    state = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: state)
    state.write_text('{"boots": [100.0, 110.0]}', encoding="utf-8")

    assert guard.check_and_record(max_restarts=3, now=120.0) is True


def test_planned_marker_must_be_recent_relative_to_boot(tmp_path):
    marker = tmp_path / ".restart_pending.json"
    marker.write_text('{"requested_at": 1000.0}', encoding="utf-8")

    assert guard.planned_marker_is_recent(marker, now=1100.0, max_gap_seconds=300) is True
    assert guard.planned_marker_is_recent(marker, now=1301.0, max_gap_seconds=300) is False
    marker.write_text('{"via_service": true}', encoding="utf-8")
    assert guard.planned_marker_is_recent(marker, now=1000.0) is False
    assert guard.planned_marker_is_recent(tmp_path / "missing.json", now=1000.0) is False


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
