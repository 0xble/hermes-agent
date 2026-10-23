"""An eager title attempt must not require a second user turn to recover."""

import json
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.title_generator import wait_for_title_upgrades
from agent.turn_context import _maybe_title_session_at_turn_start, start_deferred_title_upgrade
from hermes_state import SessionDB


@pytest.mark.parametrize("first_title, manual_title, expected_calls", [
    (None, None, 2),
    ("Unspecified Subject", None, 2),
    ("Topic Opening Message Formatting", None, 1),
    (None, "My Manual Title", 1),
])
def test_first_turn_completion_recovers_an_unusable_eager_title(
    tmp_path, monkeypatch, first_title, manual_title, expected_calls,
):
    caption = "Fix bad formatting here and improve formatting when this happens overall. Make shorter, and use toggle for any details"
    reply = "The topic opening message merges its title and objective into one large heading."
    image_note = "[The user sent an image~ Here's what I can see:\nA screenshot of a topic opening message.]"
    history = [{"role": "user", "content": image_note + "\n\n" + caption}]
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="photo-topic", source="telegram")
    callback = Mock()
    agent = SimpleNamespace(_session_db=db, session_id="photo-topic", _session_db_created=True,
                            platform="telegram", _on_session_title=callback)
    generate = Mock(side_effect=[first_title, "Topic Opening Message Formatting"])
    monkeypatch.setattr("agent.title_generator.generate_title", generate)
    monkeypatch.setattr("agent.title_generator.title_upgrade_must_wait_for_turn", lambda *_: False)
    monkeypatch.setattr("agent.title_generator._auto_title_enabled", lambda: True)
    monkeypatch.setattr("agent.title_generator._model_title_upgrade_enabled", lambda: True)
    monkeypatch.setattr("agent.title_generator._hold_until_reply_enabled", lambda: True)
    try:
        _maybe_title_session_at_turn_start(agent, history, caption)
        wait_for_title_upgrades(timeout=5)
        assert generate.call_count == 1
        assert not generate.call_args.kwargs["reply_excerpt"]
        if manual_title:
            db.set_session_title("photo-topic", manual_title)
        history.append({"role": "assistant", "content": reply})
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert generate.call_count == expected_calls
        assert db.get_session_title("photo-topic") == (manual_title or "Topic Opening Message Formatting")
        if expected_calls == 2:
            assert generate.call_args.kwargs["reply_excerpt"] == reply
            assert callback.call_args.args == ("Topic Opening Message Formatting", "llm")
        # Completion is idempotent and cannot start another attempt.
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert generate.call_count == expected_calls
    finally:
        db.close()


@pytest.mark.parametrize("opener, metadata, title, manual_title", [
    ("https://youtu.be/MgRynDGk-4Y", "Software Is DEAD", "Software Is DEAD", None),
    ("https://x.com/example/status/1234567890", "Postgres connection pool", "Postgres Connection Pool", None),
    ("https://example.slack.com/archives/C123/p1234567890", None, "Q3 Billing Dispute", None),
    ("https://example.slack.com/archives/C123/p1234567890", None, "Q3 Billing Dispute", "My Manual Title"),
])
def test_link_metadata_titles_before_reply_or_recovers_once_at_completion(
    tmp_path, monkeypatch, opener, metadata, title, manual_title,
):
    history = [{"role": "user", "content": opener}]
    reply = "The subject is the Q3 billing dispute."
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="link-topic", source="telegram")
    callback = Mock()
    agent = SimpleNamespace(_session_db=db, session_id="link-topic", _session_db_created=True,
                            platform="telegram", _on_session_title=callback)
    responses = [
        {"title": title if metadata else "", "abstain": not bool(metadata)},
        {"title": title, "abstain": False},
    ]
    call = Mock(side_effect=[SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(response)), finish_reason="stop",
    )]) for response in responses])
    fetch_threads = []
    fetch = Mock(side_effect=lambda *_: fetch_threads.append(get_ident()) or metadata)
    monkeypatch.setattr("agent.title_generator.call_llm", call)
    monkeypatch.setattr("agent.title_generator._title_config", dict)
    monkeypatch.setattr("agent.title_generator._title_link_context", fetch)
    try:
        _maybe_title_session_at_turn_start(agent, history)
        wait_for_title_upgrades(timeout=5)
        # Even an opaque URL gets a background metadata attempt before the reply exists.
        fetch.assert_called_once_with(opener, None)
        assert fetch_threads[0] != get_ident()
        assert call.call_count == 1
        first_input = call.call_args.kwargs["messages"][-1]["content"]
        assert reply not in first_input
        if metadata:
            assert metadata in first_input
            assert db.get_session_title("link-topic") == title
            assert db.get_session_title_source("link-topic") == "llm"
        else:
            assert db.get_session_title_source("link-topic") != "llm"

        if manual_title:
            db.set_session_title("link-topic", manual_title)
        history.append({"role": "assistant", "content": reply})
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        expected_calls = 1 if metadata or manual_title else 2
        assert call.call_count == expected_calls
        assert db.get_session_title("link-topic") == (manual_title or title)
        assert db.get_session_title_source("link-topic") == ("user" if manual_title else "llm")
        if expected_calls == 2:
            assert reply in call.call_args.kwargs["messages"][-1]["content"]
        llm_callbacks = [event.args for event in callback.call_args_list if event.args[1] == "llm"]
        assert llm_callbacks == ([] if manual_title else [(title, "llm")])
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert call.call_count == expected_calls
    finally:
        wait_for_title_upgrades(timeout=5)
        db.close()


@pytest.mark.parametrize("rejected_title", ["", "Investigation", "Existing Billing Topic"])
@pytest.mark.parametrize("manual_during_generation", [False, True])
def test_third_turn_finalizes_current_subject_once_without_clobbering_manual_title(
    tmp_path, monkeypatch, rejected_title, manual_during_generation,
):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="active", source="telegram")
    db.create_session(session_id="taken", source="telegram")
    db.set_session_title("taken", "Existing Billing Topic")
    callback = Mock()
    agent = SimpleNamespace(_session_db=db, session_id="active", _session_db_created=True,
                            platform="telegram", _on_session_title=callback)
    current = "Q3 invoice numbers do not match"
    history = []

    def reject_title(message, **_kwargs):
        if manual_during_generation and message == current:
            db.set_session_title("active", "My Manual Title")
        return rejected_title

    generate = Mock(side_effect=reject_title)
    monkeypatch.setattr("agent.title_generator.generate_title", generate)
    monkeypatch.setattr("agent.title_generator._title_config", dict)
    monkeypatch.setattr("agent.title_generator.title_upgrade_must_wait_for_turn", lambda *_: False)
    try:
        for index, message in enumerate(["look at this", "look again", current]):
            history.append({"role": "user", "content": message})
            _maybe_title_session_at_turn_start(agent, history)
            wait_for_title_upgrades(timeout=5)
            if index < 2:
                assert db.get_session_title("active") == "look at this"
                assert db.get_session_title_source("active") == "derived"
            history.append({"role": "assistant", "content": "The subject is not clear yet."})
            start_deferred_title_upgrade(agent)
            wait_for_title_upgrades(timeout=5)
        assert db.get_session_title("active") == ("My Manual Title" if manual_during_generation else current)
        assert db.get_session_title_source("active") == ("user" if manual_during_generation else "llm")
        final_callbacks = [event.args for event in callback.call_args_list if event.args[1] == "llm"]
        assert final_callbacks == ([] if manual_during_generation else [(current, "llm")])
        attempts = generate.call_count
        history.append({"role": "user", "content": "A different subject"})
        _maybe_title_session_at_turn_start(agent, history)
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert generate.call_count == attempts
        assert db.get_session_title("active") == ("My Manual Title" if manual_during_generation else current)
    finally:
        wait_for_title_upgrades(timeout=5)
        db.close()


@pytest.mark.parametrize("existing, current", [
    ("Q3 Billing #2", "Q3 Billing"),
    ("Influence and Persuasion", "influence and persuasion"),
    ("Exact Subject", "Exact Subject"),
    ("Other Subject", "Investigation"),
])
@pytest.mark.parametrize("manual_before_recovery", [False, True])
def test_rejected_terminal_fallback_stays_provisional_and_recovers_without_more_model_calls(
    tmp_path, monkeypatch, existing, current, manual_before_recovery,
):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="active", source="telegram")
    for session_id, title in [("taken", existing), ("model-taken", "Taken Model Subject")]:
        db.create_session(session_id=session_id, source="telegram")
        db.set_session_title(session_id, title)
    callback = Mock()
    agent = SimpleNamespace(_session_db=db, session_id="active", _session_db_created=True,
                            platform="telegram", _on_session_title=callback)
    generate = Mock(return_value="Taken Model Subject")
    link_context = Mock(return_value=None)
    monkeypatch.setattr("agent.title_generator.generate_title", generate)
    monkeypatch.setattr("agent.title_generator._title_link_context", link_context)
    monkeypatch.setattr("agent.title_generator._title_config", dict)
    monkeypatch.setattr("agent.title_generator.title_upgrade_must_wait_for_turn", lambda *_: False)
    history = []
    try:
        for message in ["look at this", "look again", current]:
            history.append({"role": "user", "content": message})
            _maybe_title_session_at_turn_start(agent, history)
            wait_for_title_upgrades(timeout=5)
            history.append({"role": "assistant", "content": "The subject is not clear yet."})
            start_deferred_title_upgrade(agent)
            wait_for_title_upgrades(timeout=5)
        assert db.get_session_title("active") == "look at this"
        assert db.get_session_title_source("active") == "derived"
        assert [event.args for event in callback.call_args_list if event.args[1] == "llm"] == []
        attempts = generate.call_count
        assert attempts == 4  # first attempt + completion retry, then turns 2 and 3
        assert link_context.call_count == attempts
        if manual_before_recovery:
            db.set_session_title("active", "My Manual Title")
        recovery = "Q4 Invoice Reconciliation"
        history.append({"role": "user", "content": recovery})
        _maybe_title_session_at_turn_start(agent, history)
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert generate.call_count == attempts
        assert link_context.call_count == attempts
        assert db.get_session_title("active") == ("My Manual Title" if manual_before_recovery else recovery)
        assert db.get_session_title_source("active") == ("user" if manual_before_recovery else "llm")
        final_callbacks = [event.args for event in callback.call_args_list if event.args[1] == "llm"]
        assert final_callbacks == ([] if manual_before_recovery else [(recovery, "llm")])
        history.append({"role": "user", "content": "A different subject"})
        _maybe_title_session_at_turn_start(agent, history)
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert generate.call_count == attempts
        assert db.get_session_title("active") == ("My Manual Title" if manual_before_recovery else recovery)
    finally:
        wait_for_title_upgrades(timeout=5)
        db.close()


@pytest.mark.parametrize("reply, retry_title, expected_title, expected_source", [
    ("Hi! How can I help?", "Friendly greeting", "hi", "derived"),
    ("Let's look at your Postgres connection pool.", "Postgres Connection Pool Exhaustion",
     "Postgres Connection Pool Exhaustion", "llm"),
])
def test_greeting_placeholder_retries_with_the_reply(
    tmp_path, monkeypatch, reply, retry_title, expected_title, expected_source,
):
    history = [{"role": "user", "content": "hi"}]
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="greeting", source="telegram")
    agent = SimpleNamespace(_session_db=db, session_id="greeting", _session_db_created=True,
                            platform="telegram")
    generate = Mock(side_effect=["Friendly greeting", retry_title])
    monkeypatch.setattr("agent.title_generator.generate_title", generate)
    monkeypatch.setattr("agent.title_generator._title_config", dict)
    monkeypatch.setattr("agent.title_generator.title_upgrade_must_wait_for_turn", lambda *_: False)
    try:
        _maybe_title_session_at_turn_start(agent, history)
        wait_for_title_upgrades(timeout=5)
        assert db.get_session_title_source("greeting") == "derived"
        history.append({"role": "assistant", "content": reply})
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert generate.call_count == 2
        assert generate.call_args.kwargs["reply_excerpt"] == reply
        assert db.get_session_title("greeting") == expected_title
        assert db.get_session_title_source("greeting") == expected_source
    finally:
        db.close()


@pytest.mark.parametrize("eager_fails", [False, True])
@pytest.mark.parametrize("failure_sink", ["_emit_auxiliary_failure", "_title_failure_callback"])
def test_completion_attempts_report_only_the_first_failure(
    tmp_path, monkeypatch, eager_fails, failure_sink,
):
    eager_error, retry_error = RuntimeError("eager unavailable"), RuntimeError("retry unavailable")
    abstention = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"title": "", "abstain": true}'), finish_reason="stop",
    )])
    call = Mock(side_effect=[eager_error if eager_fails else abstention, retry_error])
    monkeypatch.setattr("agent.title_generator.call_llm", call)
    monkeypatch.setattr("agent.title_generator._title_config", dict)
    monkeypatch.setattr("agent.title_generator.title_upgrade_must_wait_for_turn", lambda *_: False)
    history = [{"role": "user", "content": "Fix topic opening formatting"}]
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="failed-title", source="cli")
    report_failure = Mock()
    agent = SimpleNamespace(_session_db=db, session_id="failed-title", _session_db_created=True,
                            platform="cli", **{failure_sink: report_failure})
    try:
        _maybe_title_session_at_turn_start(agent, history)
        wait_for_title_upgrades(timeout=5)
        assert report_failure.call_count == int(eager_fails)
        history.append({"role": "assistant", "content": "The title and objective share a heading."})
        start_deferred_title_upgrade(agent)
        wait_for_title_upgrades(timeout=5)
        assert call.call_count == 2
        report_failure.assert_called_once_with("title generation", eager_error if eager_fails else retry_error)
    finally:
        db.close()


@pytest.mark.parametrize("hold, user_turns, has_reply, retries", [
    (False, 1, True, False),
    (True, 2, True, False),
    (True, 1, False, False),
    (True, 1, True, True),
])
def test_completion_retry_is_bounded_and_waits_off_the_turn_path(
    monkeypatch, hold, user_turns, has_reply, retries,
):
    from agent.title_generator import defer_title_completion_retry, start_title_upgrade

    started, release = Event(), Event()

    def eager_attempt():
        started.set()
        assert release.wait(timeout=5)

    eager = Thread(target=eager_attempt)
    eager.start()
    assert started.wait(timeout=5)
    history = [{"role": "user", "content": "Fix topic opening formatting"} for _ in range(user_turns)]
    generate = Mock()
    monkeypatch.setattr("agent.title_generator._hold_until_reply_enabled", lambda: hold)
    monkeypatch.setattr("agent.title_generator._has_upgraded_title", lambda *_: False)
    monkeypatch.setattr("agent.title_generator.auto_title_session", generate)
    try:
        deferred = defer_title_completion_retry(
            eager, object(), "topic", "Fix topic opening formatting", conversation_history=history,
        )
        if has_reply:
            history.append({"role": "assistant", "content": "The title and objective share a heading."})
        if deferred is not None:
            # Starts without joining the pending eager request on the main turn thread.
            start_title_upgrade(deferred)
        generate.assert_not_called()
        release.set()
        eager.join(timeout=5)
        wait_for_title_upgrades(timeout=5)
        assert generate.called is retries
    finally:
        release.set()
        eager.join(timeout=5)
        wait_for_title_upgrades(timeout=5)
