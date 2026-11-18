"""Regenerable goal wakes must not become immortal shutdown follow-up spools."""

import asyncio
import json
from dataclasses import replace

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run_pending_recovery import recover_pending_shutdown_flush
from gateway.shutdown_flush import flush_pending_to_file
from tests.gateway.test_shutdown_followup_restart import _setup


@pytest.mark.parametrize("reason", ["shutdown_follow_up", "turn_lease_wait", "shutdown"])
@pytest.mark.parametrize("route_available", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
def test_shutdown_goal_wake_retires_once_without_replay_or_notice(
        tmp_path, monkeypatch, caplog, reason, route_available, legacy):
    from hermes_cli.goals import GoalManager, GoalState

    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    state = GoalState(goal="old goal", created_at=1.0, last_turn_at=3.0)
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: state)
    prompt = GoalManager._current_continuation_prompt(state)
    event = runner._synthetic_prompt_event(
        source, prompt, reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_fingerprint="v2:old", goal_instance=1.0,
    )
    event.metadata["goal_continuation_created_at"] = 2.0
    assert flush_pending_to_file({entry.session_key: event}, reason=reason) == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    if legacy:
        payload = json.loads(spool.read_text(encoding="utf-8"))
        payload["data"].pop("goal_continuation_only", None)
        spool.write_text(json.dumps(payload), encoding="utf-8")
    # No delivery route should be necessary; legacy entries use the goal state only.
    if not route_available:
        runner.session_store.resolve_session_id_for_key = lambda *args, **kwargs: None
        runner._delivery_adapter_for = lambda source: None
    before = runner.session_store.load_transcript(entry.session_id)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert recover_pending_shutdown_flush(runner, candidates=[]) == 0
        assert not spool.exists()
        assert recover_pending_shutdown_flush(runner, candidates=[]) == 0
    assert runner._startup_restore_queue == []
    assert runner.session_store.load_transcript(entry.session_id) == before
    assert adapter.sent == []
    outcomes = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [
        f"Restart recovery outcome: session={entry.session_key} "
        "outcome=skipped:superseded-goal-continuation"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["quoted", "idless", "merged", "transport", "attachment", "lookalike"])
async def test_real_followup_survives_non_turn_and_is_delivered_only_once(
        tmp_path, monkeypatch, kind):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(
        text="continue the old goal", source=source, user_id=source.user_id,
        reply_expected=False if kind != "merged" else True,
    )
    if kind in {"merged", "transport", "attachment", "lookalike"}:
        event.metadata = {"goal_continuation": True}
    if kind in {"quoted", "transport"}:
        event.message_id = "human-transport-id"
        event.source = replace(source, message_id=event.message_id)
    if kind == "attachment":
        event.media_urls = ["photo.png"]
    if kind == "lookalike":
        from hermes_cli.goals import GoalManager, GoalState

        state = GoalState(goal="old goal", created_at=1.0)
        monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: state)
        event.text = GoalManager._current_continuation_prompt(state)
        event.metadata.update(goal_continuation_session_id=entry.session_id,
                              goal_continuation_instance=state.created_at)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    original = spool.read_bytes()

    # An adapter receipt is not proof a user's input ran. Keep the only copy even
    # when the notice cannot be delivered, exactly as in the incident's skip path.
    async def accept_without_turn(recovered):
        recovered._gateway_accepted = True

    adapter.handle_message = accept_without_turn
    runner._deliver_recovered_followup_note = lambda *args, **kwargs: _false()
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 1
    await runner._drain_startup_restore_queue()
    await asyncio.gather(*list(runner._background_tasks))
    assert spool.read_bytes() == original

    # A fresh gateway can deliver that same real follow-up. Persist its identity
    # before the turn finishes, and verify subsequent recovery never replays it.
    fresh, adapter, entry, _source = _setup(tmp_path, monkeypatch)
    delivered = []

    async def complete(recovered):
        delivered.append(recovered.text)
        fresh.session_store.append_to_transcript(entry.session_id, {
            "role": "user", "content": recovered.text,
            "platform_message_id": recovered.message_id or recovered._hermes_recovery_id,
        })
        recovered._gateway_accepted = True
        recovered._agent_turn_succeeded = True

    adapter.handle_message = complete
    assert recover_pending_shutdown_flush(fresh, candidates=[]) == 1
    assert await fresh._drain_startup_restore_queue() == 1
    await asyncio.gather(*list(fresh._background_tasks))
    assert not spool.exists()
    assert recover_pending_shutdown_flush(fresh, candidates=[]) == 0
    assert fresh._startup_restore_queue == []
    assert delivered == [event.text]
    assert adapter.sent == []
    rows = fresh.session_store.load_transcript(entry.session_id)
    assert [row["content"] for row in rows if row["role"] == "user"] == [event.text]


@pytest.mark.parametrize("legacy", [True, False])
def test_merged_human_text_is_not_retired_with_goal_wake(tmp_path, monkeypatch, legacy):
    from gateway.platforms.base import merge_pending_message_event
    from hermes_cli.goals import GoalManager, GoalState

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    state = GoalState(goal="old goal", created_at=1.0)
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: state)
    prompt = GoalManager._current_continuation_prompt(state)
    wake = runner._synthetic_prompt_event(
        source, prompt, reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_instance=1.0,
    )
    pending = {entry.session_key: wake}
    # Legacy adapters can leave the nullable reply contract unspecified; don't
    # let a synthetic queue head hide this absorbed, id-less human input.
    human = MessageEvent(text="also please check my changes", source=source)
    merge_pending_message_event(pending, entry.session_key, human, merge_text=True)
    assert flush_pending_to_file(pending, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    if legacy:
        payload = json.loads(spool.read_text(encoding="utf-8"))
        payload["data"].pop("goal_continuation_only", None)
        spool.write_text(json.dumps(payload), encoding="utf-8")
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 1
    assert spool.exists()
    assert len(runner._startup_restore_queue) == 1
    assert human.text in runner._startup_restore_queue[0].text


def test_real_followup_survives_transient_recovery_error(tmp_path, monkeypatch):
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="please do not lose this", source=source)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    original = spool.read_bytes()
    resolver = runner.session_store.resolve_session_id_for_key

    def unavailable(*args, **kwargs):
        raise OSError("temporary session-store outage")

    runner.session_store.resolve_session_id_for_key = unavailable
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 0
    assert spool.read_bytes() == original
    runner.session_store.resolve_session_id_for_key = resolver
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 1
    assert len(runner._startup_restore_queue) == 1


def test_synthetic_codec_copy_keeps_absorbed_human_provenance(tmp_path, monkeypatch):
    from gateway.shutdown_flush import _serialise_value

    runner, _adapter, _entry, source = _setup(tmp_path, monkeypatch)
    event = runner._synthetic_prompt_event(
        source, "an old generated prompt", reply_expected=False,
        goal_continuation=True,
    )
    # The codec can carry a flattened merge trail without the original objects.
    event._native_absorbed_synthetic = [None]
    assert _serialise_value(event)["goal_continuation_only"] is False
    event._native_absorbed_synthetic = None
    assert _serialise_value(event)["goal_continuation_only"] is False


def test_new_goal_wake_retires_even_when_goal_was_cleared(tmp_path, monkeypatch):
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = runner._synthetic_prompt_event(
        source, "an old generated prompt", reply_expected=False,
        goal_continuation=True, goal_session_id=entry.session_id,
    )
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: None)
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 0
    assert not spool.exists()
    assert runner._startup_restore_queue == []


def test_ambiguous_legacy_goal_spool_is_preserved(tmp_path, monkeypatch):
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = runner._synthetic_prompt_event(
        source, "prompt and possibly absorbed human text", reply_expected=False,
        goal_continuation=True, goal_session_id=entry.session_id,
    )
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    payload = json.loads(spool.read_text(encoding="utf-8"))
    payload["data"].pop("goal_continuation_only", None)
    spool.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: None)
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 1
    assert spool.exists()
    assert len(runner._startup_restore_queue) == 1


@pytest.mark.parametrize("legacy", [True, False])
def test_current_unparked_goal_keeps_its_only_outstanding_wake(tmp_path, monkeypatch, legacy):
    from hermes_cli.goals import GoalManager, GoalState

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    state = GoalState(goal="still needs its queued turn", created_at=1.0, last_turn_at=1.5)
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: state)
    event = runner._synthetic_prompt_event(
        source, GoalManager._current_continuation_prompt(state), reply_expected=False,
        goal_continuation=True, goal_session_id=entry.session_id, goal_instance=1.0,
    )
    event.metadata["goal_continuation_created_at"] = 2.0
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    if legacy:
        payload = json.loads(spool.read_text(encoding="utf-8"))
        payload["data"].pop("goal_continuation_only", None)
        spool.write_text(json.dumps(payload), encoding="utf-8")
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 1
    assert spool.exists()
    assert len(runner._startup_restore_queue) == 1


@pytest.mark.parametrize("human_suffix", ["", "\n\nPlease also keep my real follow-up."])
def test_legacy_autonomous_wake_survives_config_change(tmp_path, monkeypatch, human_suffix):
    from hermes_cli.goals import GoalManager, GoalState
    from hermes_cli.goals_autonomy import AUTONOMY_CONTINUATION_TEMPLATE

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    state = GoalState(goal="old goal", created_at=1.0, last_turn_at=3.0, subgoals=["check evidence"])
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: state)
    monkeypatch.setattr("hermes_cli.goals.goal_autonomy_enabled", lambda: False)
    prompt = AUTONOMY_CONTINUATION_TEMPLATE.format(
        goal=state.goal, contract_block=state.contract.render_block(),
        subgoals_block=state.render_subgoals_block(),
    )
    assert prompt != GoalManager._current_continuation_prompt(state)
    event = runner._synthetic_prompt_event(
        source, prompt + human_suffix, reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_instance=1.0,
    )
    event.metadata["goal_continuation_created_at"] = 2.0
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    payload = json.loads(spool.read_text(encoding="utf-8"))
    payload["data"].pop("goal_continuation_only", None)
    spool.write_text(json.dumps(payload), encoding="utf-8")
    assert recover_pending_shutdown_flush(runner, candidates=[]) == bool(human_suffix)
    assert spool.exists() == bool(human_suffix)


def test_unavailable_goal_store_does_not_retire_outstanding_wake(tmp_path, monkeypatch):
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = runner._synthetic_prompt_event(
        source, "the only outstanding wake", reply_expected=False,
        goal_continuation=True, goal_session_id=entry.session_id,
    )
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    monkeypatch.setattr("hermes_cli.goals._get_session_db", lambda: None)
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 1
    assert spool.exists()
    assert len(runner._startup_restore_queue) == 1


def test_transitive_pure_goal_merge_is_not_mistaken_for_human_input(tmp_path, monkeypatch):
    from gateway.platforms.base import merge_pending_message_event
    from gateway.shutdown_flush import _serialise_value

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    wakes = [runner._synthetic_prompt_event(
        source, "goal wake", reply_expected=False, goal_continuation=True,
    ) for _ in range(3)]
    pending = {entry.session_key: wakes[1]}
    merge_pending_message_event(pending, entry.session_key, wakes[2], merge_text=True)
    pending[entry.session_key] = wakes[0]
    merge_pending_message_event(pending, entry.session_key, wakes[1], merge_text=True)
    assert _serialise_value(wakes[0])["goal_continuation_only"] is True


@pytest.mark.parametrize("absorbed_created_at", [2.5, 4.0])
def test_every_merged_goal_wake_must_be_superseded(tmp_path, monkeypatch, absorbed_created_at):
    from hermes_cli.goals import GoalState
    from gateway.platforms.base import merge_pending_message_event
    from gateway.shutdown_flush import _serialise_value
    from gateway.shutdown_goal_wakes import regenerable_goal_continuation

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    state = GoalState(goal="old goal", created_at=1.0, last_turn_at=3.0, waiting_until=5.0)
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: state)
    wakes = [runner._synthetic_prompt_event(
        source, "goal wake", reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_instance=1.0,
    ) for _ in range(2)]
    for wake, created_at in zip(wakes, [2.0, absorbed_created_at]):
        wake.metadata["goal_continuation_created_at"] = created_at
    pending = {entry.session_key: wakes[0]}
    merge_pending_message_event(pending, entry.session_key, wakes[1], merge_text=True)
    serialized = _serialise_value(wakes[0])
    assert serialized is not None
    assert regenerable_goal_continuation(serialized) == (absorbed_created_at < 3.0)


def test_superseded_active_wake_has_durable_polling_owner_before_retirement(tmp_path, monkeypatch):
    from hermes_cli.goals import GoalState, load_goal, save_goal

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    save_goal(entry.session_id, GoalState(goal="old goal", created_at=1.0, last_turn_at=3.0))
    event = runner._synthetic_prompt_event(
        source, "old wake", reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_instance=1.0,
    )
    event.metadata["goal_continuation_created_at"] = 2.0
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 0
    state = load_goal(entry.session_id)
    assert state is not None
    assert state.waiting_until > 0  # The idle poller can now regenerate a current wake after a crash.
    assert state.goal == "old goal"
    assert state.created_at == 1.0
    assert state.last_turn_at == 3.0
    assert not list((tmp_path / "pending_messages").glob("*.json"))


def test_goal_owner_failure_keeps_spool(tmp_path, monkeypatch):
    from hermes_cli.goals import GoalState, _get_session_db, save_goal

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    save_goal(entry.session_id, GoalState(goal="old goal", created_at=1.0, last_turn_at=3.0))
    event = runner._synthetic_prompt_event(
        source, "old wake", reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_instance=1.0,
    )
    event.metadata["goal_continuation_created_at"] = 2.0
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    db = _get_session_db()
    assert db is not None

    def fail_write(transaction):
        raise OSError("store unavailable")

    monkeypatch.setattr(db, "_execute_write", fail_write)
    assert recover_pending_shutdown_flush(runner, candidates=[]) == 1
    assert spool.exists()
    assert len(runner._startup_restore_queue) == 1


@pytest.mark.parametrize("human_suffix", ["", "\n\nKeep this human message."])
def test_legacy_plain_wake_after_autonomy_enabled(tmp_path, monkeypatch, human_suffix):
    from hermes_cli.goals import GoalManager, GoalState

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    state = GoalState(goal="old goal", created_at=1.0, last_turn_at=3.0)
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda sid, **_kwargs: state)
    monkeypatch.setattr("hermes_cli.goals.goal_autonomy_enabled", lambda: False)
    prompt = GoalManager._current_continuation_prompt(state)
    monkeypatch.setattr("hermes_cli.goals.goal_autonomy_enabled", lambda: True)
    assert prompt != GoalManager._current_continuation_prompt(state)
    event = runner._synthetic_prompt_event(
        source, prompt + human_suffix, reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_instance=1.0,
    )
    event.metadata["goal_continuation_created_at"] = 2.0
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    payload = json.loads(spool.read_text(encoding="utf-8"))
    payload["data"].pop("goal_continuation_only", None)
    spool.write_text(json.dumps(payload), encoding="utf-8")
    assert recover_pending_shutdown_flush(runner, candidates=[]) == bool(human_suffix)
    assert spool.exists() == bool(human_suffix)


@pytest.mark.parametrize("child_last_turn", [0.0, 3.0])
def test_compression_archived_goal_keeps_valid_child_wake(tmp_path, monkeypatch, child_last_turn):
    from hermes_cli.goals import GoalState, _get_session_db, load_goal, migrate_goal_to_session, save_goal

    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    db = _get_session_db()
    assert db is not None
    db.create_session(entry.session_id, "telegram")
    save_goal(entry.session_id, GoalState(goal="survive compression", created_at=1.0))
    event = runner._synthetic_prompt_event(
        source, "only outstanding goal wake", reply_expected=False, goal_continuation=True,
        goal_session_id=entry.session_id, goal_instance=1.0,
    )
    event.metadata["goal_continuation_created_at"] = 2.0
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db.end_session(entry.session_id, "compression")
    db.create_session("compressed-child", "telegram", parent_session_id=entry.session_id)
    assert migrate_goal_to_session(entry.session_id, "compressed-child", reason="compression")
    state = load_goal("compressed-child")
    assert state is not None
    state.last_turn_at = child_last_turn
    save_goal("compressed-child", state)
    spool, = (tmp_path / "pending_messages").glob("*.json")
    assert recover_pending_shutdown_flush(runner, candidates=[]) == (child_last_turn < 2.0)
    assert spool.exists() == (child_last_turn < 2.0)
    state = load_goal("compressed-child")
    assert state is not None
    assert bool(state.waiting_until) == (child_last_turn > 2.0)


async def _false():
    return False
