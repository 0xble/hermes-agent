"""Title workers bind the privacy state of the session whose history they read."""
import pytest

from agent import title_generator as titles
from agent.incognito import incognito_scope, register_session
from hermes_state import SessionDB
from tests.agent.test_incognito_auxiliary_health import _files


def _fake_generator(monkeypatch, results):
    calls = []
    answers = iter(results)

    def generate(*args, **kwargs):
        calls.append((args, kwargs))
        return next(answers)

    monkeypatch.setattr(titles, "generate_title", generate)
    monkeypatch.setattr(titles, "_auto_title_enabled", lambda: True)
    monkeypatch.setattr(titles, "_title_config", dict)
    monkeypatch.setattr(titles, "_model_title_upgrade_enabled", lambda: True)
    monkeypatch.setattr(titles, "_hold_until_reply_enabled", lambda: True)
    return calls


@pytest.mark.parametrize("completion_retry", [False, True])
@pytest.mark.parametrize("exit_before_start", [False, True])
def test_deferred_public_title_worker_cannot_cross_private_boundary(
    tmp_path, monkeypatch, completion_retry, exit_before_start,
):
    from unittest.mock import Mock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    answers = [None, "Public completed title"] if completion_retry else ["Public completed title"]
    calls = _fake_generator(monkeypatch, answers)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("boundary-title", "telegram", model="main")
    db.append_message("boundary-title", "user", "public prefix")
    prefix = db.get_messages("boundary-title")
    callback = Mock()
    history = [{"role": "user", "content": "public subject"}]
    upgrade = deferred = fresh = None
    try:
        # Hosted work starts eagerly. An undeclared custom route stays deferred.
        runtime = {"provider": "openrouter" if completion_retry else "custom",
                   "base_url": "https://title.test/v1" if completion_retry else "http://localhost:8000/v1"}
        upgrade = titles.maybe_auto_title(
            db, "boundary-title", "public subject", conversation_history=history,
            main_runtime=runtime, title_callback=callback,
        )
        assert upgrade is not None
        if completion_retry:
            assert upgrade.ident is not None
            upgrade.join(timeout=5)
            assert not upgrade.is_alive()
            assert len(calls) == 1  # The eager original deliberately abstained.
        else:
            assert upgrade.ident is None
            assert calls == []
        deferred = titles.defer_title_completion_retry(
            upgrade, db, "boundary-title", "public subject",
            conversation_history=history, title_callback=callback,
        )
        assert deferred is not None and deferred.ident is None
        assert (deferred is not upgrade) is completion_retry
        expected_calls = len(calls)
        callback.reset_mock()
        title = db.get_session_title("boundary-title")
        before = _files(tmp_path)
        register_session("boundary-title")
        history.append({"role": "assistant", "content": "PRIVATE_TITLE_REPLY_CANARY"})
        if exit_before_start:
            register_session("boundary-title", False)
        # Publicly created work must also reject a completely missed interval.
        titles.start_title_upgrade(deferred)
        deferred.join(timeout=5)
        assert not deferred.is_alive()
        assert len(calls) == expected_calls
        callback.assert_not_called()
        assert db.get_session_title("boundary-title") == title
        assert db.get_messages("boundary-title") == prefix
        assert _files(tmp_path) == before

        register_session("boundary-title", False)
        fresh = titles.maybe_auto_title(
            db, "boundary-title", "public subject", title_callback=callback,
            conversation_history=[{"role": "user", "content": "public subject"},
                                  {"role": "assistant", "content": "public completed subject"}],
            main_runtime={"provider": "openrouter", "base_url": "https://title.test/v1"},
        )
        assert fresh is not None
        titles.start_title_upgrade(fresh)
        fresh.join(timeout=5)
        assert not fresh.is_alive()
        assert len(calls) == expected_calls + 1
        assert db.get_session_title("boundary-title") == "Public completed title"
        assert callback.call_args.args == ("Public completed title", "llm")
        db.append_message("boundary-title", "assistant", "public suffix")
        rows = db.get_messages("boundary-title")
        assert rows[:-1] == prefix and rows[-1]["content"] == "public suffix"
        assert all(b"PRIVATE_TITLE_REPLY_CANARY" not in data for data in _files(tmp_path).values())
    finally:
        register_session("boundary-title", False)
        for worker in (upgrade, deferred, fresh):
            if worker is not None and worker.ident is not None:
                worker.join(timeout=5)
                assert not worker.is_alive()
        db.close()


@pytest.mark.parametrize("boundary", ["on", "on_off", "anonymous"])
def test_completion_retry_preserves_its_original_and_callers_privacy(tmp_path, monkeypatch, boundary):
    from contextlib import nullcontext
    from unittest.mock import Mock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _fake_generator(monkeypatch, [None, "Private title"])
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("retry-origin", "telegram", model="main")
    callback = Mock()
    history = [{"role": "user", "content": "public subject"}]
    worker = None
    try:
        worker = titles.maybe_auto_title(
            db, "retry-origin", "public subject", conversation_history=history,
            main_runtime={"provider": "openrouter", "base_url": "https://title.test/v1"},
            title_callback=callback,
        )
        assert worker is not None and worker.ident is not None
        worker.join(timeout=5)
        assert not worker.is_alive() and len(calls) == 1
        callback.reset_mock()
        before = _files(tmp_path)
        if boundary != "anonymous":
            register_session("retry-origin")
        with incognito_scope() if boundary == "anonymous" else nullcontext():
            history.append({"role": "assistant", "content": "PRIVATE_RETRY_ORIGIN_CANARY"})
            if boundary == "on_off":
                register_session("retry-origin", False)
            assert titles.defer_title_completion_retry(
                worker, db, "retry-origin", "public subject", conversation_history=history,
                title_callback=callback,
            ) is None
        assert len(calls) == 1
        callback.assert_not_called()
        assert _files(tmp_path) == before
    finally:
        register_session("retry-origin", False)
        if worker is not None and worker.ident is not None:
            worker.join(timeout=5)
            assert not worker.is_alive()
        db.close()


def test_direct_unscoped_completion_retry_captures_before_scheduling(tmp_path, monkeypatch):
    from threading import Thread
    from unittest.mock import Mock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _fake_generator(monkeypatch, ["Private title"])
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("direct-retry", "telegram", model="main")
    original = Thread(target=lambda: None)
    original.start()
    original.join(timeout=5)
    callback = Mock()
    retry = None
    try:
        assert not original.is_alive()
        retry = titles.defer_title_completion_retry(
            original, db, "direct-retry", "public subject", title_callback=callback,
            conversation_history=[{"role": "user", "content": "public subject"},
                                  {"role": "assistant", "content": "PRIVATE_DIRECT_RETRY_CANARY"}],
        )
        assert retry is not None and retry.ident is None
        before = _files(tmp_path)
        register_session("direct-retry")
        register_session("direct-retry", False)
        titles.start_title_upgrade(retry)
        retry.join(timeout=5)
        assert not retry.is_alive()
        assert calls == []
        callback.assert_not_called()
        assert _files(tmp_path) == before
    finally:
        register_session("direct-retry", False)
        if retry is not None and retry.ident is not None:
            retry.join(timeout=5)
            assert not retry.is_alive()
        db.close()


def test_private_title_completion_is_inert_but_public_completion_persists(tmp_path, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _fake_generator(monkeypatch, ["Public completed title"])
    link_context = Mock(return_value="")
    monkeypatch.setattr(titles, "_title_link_context", link_context)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("private-title", "telegram", model="main")
    history = [{"role": "user", "content": "https://example.test/doc"}, {"role": "assistant", "content": "completed subject"}]
    try:
        before = _files(tmp_path)
        with incognito_scope():
            worker = titles.maybe_auto_title(db, "private-title", history[0]["content"], conversation_history=history)
            if worker is not None:
                titles.start_title_upgrade(worker)
                worker.join(timeout=5)
                assert not worker.is_alive()
        assert calls == []
        link_context.assert_not_called()
        assert _files(tmp_path) == before
        worker = titles.maybe_auto_title(db, "private-title", history[0]["content"], conversation_history=history)
        assert worker is not None
        titles.start_title_upgrade(worker)
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert db.get_session_title("private-title") == "Public completed title"
        assert len(calls) == 1
        link_context.assert_called_once()
    finally:
        db.close()


@pytest.mark.parametrize("exit_before_completion", [False, True])
def test_public_title_worker_already_inflight_observes_private_transition(tmp_path, monkeypatch, exit_before_completion):
    from threading import Event
    from unittest.mock import Mock
    from agent import auxiliary_client as aux
    from agent.incognito import is_incognito

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_generator(monkeypatch, [])
    entered, release = Event(), Event()
    observations = []

    def generate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        observations.append((is_incognito(), kwargs.get("reply_excerpt")))
        aux._mark_provider_unhealthy("custom", base_url="https://private-title.test/v1", model="private-title-model", reason="payment error")
        return "Private response title"

    monkeypatch.setattr(titles, "generate_title", generate)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("inflight-title", "telegram", model="main")
    callback = Mock()
    history = [{"role": "user", "content": "Fix the public database"}]
    worker = titles.maybe_auto_title(db, "inflight-title", history[0]["content"], conversation_history=history,
                                     main_runtime={"base_url": "https://title.test/v1"}, title_callback=callback)
    try:
        assert worker is not None and entered.wait(5)
        callback.reset_mock()
        old_title = db.get_session_title("inflight-title")
        before = _files(tmp_path)
        register_session("inflight-title")
        history.append({"role": "assistant", "content": "PRIVATE_INFLIGHT_TITLE_CANARY"})
        if exit_before_completion:
            register_session("inflight-title", False)
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert observations == [(True, "")]
        callback.assert_not_called()
        assert db.get_session_title("inflight-title") == old_title
        assert _files(tmp_path) == before
    finally:
        release.set()
        if worker is not None:
            worker.join(timeout=5)
        register_session("inflight-title", False)
        db.close()


@pytest.mark.parametrize("private_init", [False, True])
def test_real_private_agent_title_prologue_and_finalizer_are_inert(tmp_path, monkeypatch, private_init):
    from unittest.mock import Mock
    from agent.turn_context import _maybe_title_session_at_turn_start, start_deferred_title_upgrade
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda **kwargs: {})
    calls = _fake_generator(monkeypatch, [])
    accounting, callback = Mock(), Mock()
    monkeypatch.setattr("agent.aux_accounting.set_accounting_context", accounting)
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = AIAgent(model="test-model", provider="custom", api_key="test-only",
                    base_url="https://title.test/v1", quiet_mode=True, enabled_toolsets=[],
                    skip_context_files=True, skip_memory=True, session_id="private-title-agent",
                    session_db=db, incognito=private_init)
    agent._on_session_title = callback
    if not private_init:
        agent.set_incognito(True)
    try:
        before = _files(tmp_path)
        history = [{"role": "user", "content": "PRIVATE_TITLE_PROLOGUE_CANARY"}]
        _maybe_title_session_at_turn_start(agent, history)
        history.append({"role": "assistant", "content": "PRIVATE_TITLE_COMPLETION_CANARY"})
        start_deferred_title_upgrade(agent)
        assert getattr(agent, "_deferred_title_upgrade", None) is None
        assert calls == []
        accounting.assert_not_called()
        callback.assert_not_called()
        assert _files(tmp_path) == before
    finally:
        agent.close()
        register_session(agent.session_id, False)
        db.close()

