"""Persistent cadence and live ownership, without a model or transport."""
from types import SimpleNamespace

import pytest

from hermes_cli.checkins import CheckinConfig, CheckinManager, checkin_prompt, outstanding_work
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.create_session("owner", source="telegram")
    yield store
    store.close()


def test_arms_only_with_work_and_disarms_when_last_worker_finishes(db):
    manager = CheckinManager(db, "owner", CheckinConfig())
    assert manager.reconcile([], now=100) is None
    state = manager.reconcile([{"id": "child"}], now=100)
    assert state.last_visible_at == 100 and state.due_at == 1300 and not state.fired
    assert manager.reconcile([{"id": "child"}], now=500).due_at == 1300
    assert manager.reconcile([], now=600) is None
    assert manager.state is None


def test_delivery_resets_first_then_recurring_cadence_and_stale_tick_cannot_start(db):
    manager = CheckinManager(db, "owner", CheckinConfig())
    manager.reconcile([1], now=100)
    manager.visible(200)
    assert manager.state.due_at == 1400
    assert not manager.start(1300, [1], now=1500)
    assert manager.start(1400, [1], now=1500)
    assert manager.state.fired and manager.state.due_at == 3300
    manager.visible(1510)
    assert manager.state.due_at == 3310
    assert not manager.start(1400, [1], now=5000)


def test_state_survives_reopen_and_one_overdue_tick_coalesces(tmp_path):
    path = tmp_path / "restart.db"
    db = SessionDB(db_path=path)
    db.create_session("owner", source="telegram")
    CheckinManager(db, "owner", CheckinConfig()).reconcile([1], now=100)
    db.close()
    db = SessionDB(db_path=path)
    try:
        manager = CheckinManager(db, "owner", CheckinConfig())
        assert manager.start(1300, [1], now=9000)
        assert not manager.start(1300, [1], now=9000)
        assert manager.state.due_at == 10800
    finally:
        db.close()


def test_disabled_config_disarms_existing_state(db):
    CheckinManager(db, "owner", CheckinConfig()).reconcile([1], now=100)
    disabled = CheckinManager(db, "owner", CheckinConfig(enabled=False))
    assert disabled.reconcile([1], now=200) is None
    assert disabled.state is None


@pytest.mark.parametrize("value,expected", [(1, 300), (-1, 300), (3600, 3600),
                                            ("bad", 1200), (None, 1200), (float("nan"), 1200),
                                            (float("inf"), 1200)])
def test_config_validates_duration_and_floor(monkeypatch, value, expected):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"checkins": {"first_after_seconds": value}})
    assert CheckinConfig.load().first_after_seconds == expected


def test_prompt_is_generated_but_its_reply_is_not_suppressible():
    from agent.synthetic_prompt import human_prompt_text, is_runtime_prompt
    from gateway.response_filters import is_machinery_display_kind, silence_allowed
    prompt = checkin_prompt([{"id": "child", "name": "test", "status": "running", "started_at": 100}],
                            CheckinConfig(), now=200)
    assert is_runtime_prompt(display_kind="supervisor_checkin")
    assert human_prompt_text(prompt) is None
    assert human_prompt_text(prompt + "\nActual human follow-up") == "Actual human follow-up"
    assert is_machinery_display_kind("supervisor_checkin")
    assert not silence_allowed("supervisor_checkin", False)
    assert silence_allowed("internal_notification")
    assert "unknown" in prompt and "age_seconds" in prompt
    assert "reaction alone is NOT valid" in prompt


def test_lineage_does_not_share_state_with_reset(db, monkeypatch):
    original = db.get_compression_lineage
    monkeypatch.setattr(db, "get_compression_lineage", lambda sid: ["owner", "compressed"]
                        if sid == "compressed" else original(sid))
    owner = CheckinManager(db, "owner", CheckinConfig())
    owner.reconcile([1], now=100)
    assert CheckinManager(db, "compressed", CheckinConfig()).state.due_at == 1300
    db.create_session("reset", source="telegram")
    assert CheckinManager(db, "reset", CheckinConfig()).state is None


def test_live_inventory_filters_ownership_and_notified_processes(db, monkeypatch):
    import threading
    from tools.process_registry import process_registry
    calls = []
    monkeypatch.setattr("tools.async_delegation.supervisor_inventory", lambda sids: calls.append(sids) or [
        {"kind": "delegation", "id": "child", "name": "compile", "status": "running"}])
    def process(sid, parent, notify):
        return SimpleNamespace(id=sid, parent_session_id=parent, notify_on_complete=notify, exited=False,
                               command="tests", started_at=10, log_path="", output_log="", _lock=threading.Lock())
    monkeypatch.setattr(process_registry, "_running", {
        "ours": process("ours", "owner", True), "old": process("old", "old-owner", True),
        "daemon": process("daemon", "owner", False)})
    assert {w["id"] for w in outstanding_work(db, "owner")} == {"child", "ours"}
    assert calls == [["owner"]]


@pytest.mark.parametrize("barrier", ["session", "pid", "timer", "user"])
def test_active_goal_only_arms_for_live_work_wait(db, monkeypatch, barrier):
    from hermes_cli.goals import GoalState
    from tools.process_registry import process_registry
    monkeypatch.setattr("tools.async_delegation.supervisor_inventory", lambda _: [])
    monkeypatch.setattr(process_registry, "_running", {})
    monkeypatch.setattr("hermes_cli.goals._pid_alive", lambda _: True)
    monkeypatch.setattr("hermes_cli.goals._process_outcome", lambda _: {"running": True})
    state = GoalState(goal="ship", created_at=10, last_turn_at=20,
                      **{"session": {"waiting_on_session": "peer"}, "pid": {"waiting_on_pid": 42},
                         "timer": {"waiting_until": 2000}, "user": {"waiting_on_user_message": True}}[barrier])
    db.set_meta("goal:owner", state.to_json())
    work = outstanding_work(db, "owner")
    assert bool(work) == (barrier in {"session", "pid"})
    if work:
        assert work[0]["name"] == "ship"


def test_inventory_failure_is_not_completion(db, monkeypatch):
    def unavailable(_):
        raise OSError("registry unavailable")
    monkeypatch.setattr("tools.async_delegation.supervisor_inventory", unavailable)
    manager = CheckinManager(db, "owner", CheckinConfig())
    manager.reconcile([1], now=100)
    with pytest.raises(OSError):
        outstanding_work(db, "owner")
    assert manager.state.due_at == 1300
