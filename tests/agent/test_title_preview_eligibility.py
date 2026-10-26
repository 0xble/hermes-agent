"""Paste subjects must reach the actual instant-title and upgrade boundaries."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from agent import title_generator as titles
from hermes_state import SessionDB


@pytest.mark.parametrize("model_upgrade", [False, True])
@pytest.mark.parametrize("opener, preview, eligible", [
    ("@file:/pastes/opener.txt", "Postgres connection pool exhaustion", True),
    ("@file:/pastes/opener.txt", None, False),
    ("@file:/pastes/opener.txt", "   ", False),
    ("@file:/pastes/opener.txt", "@file:/other/attachment.txt", False),
    ("[CONTEXT COMPACTION] synthetic handoff", "Postgres connection pool exhaustion", False),
])
def test_paste_subject_reaches_instant_title_and_model_upgrade(
    tmp_path, monkeypatch, model_upgrade, opener, preview, eligible,
):
    monkeypatch.setattr(titles, "_title_config", lambda: {"model_upgrade_enabled": model_upgrade})
    monkeypatch.setattr(titles, "title_upgrade_must_wait_for_turn", lambda *_: False)
    generate = Mock(return_value="Postgres Pool Exhaustion")
    monkeypatch.setattr(titles, "generate_title", generate)
    callback = Mock()
    history = [{"role": "user", "content": opener}]
    original = deepcopy(history)
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="paste", source="telegram")
    try:
        upgrade = titles.maybe_auto_title(
            db, "paste", opener, conversation_history=history,
            title_preview=preview, title_callback=callback,
        )
        titles.wait_for_title_upgrades(timeout=5)
        assert (upgrade is not None) is (eligible and model_upgrade)
        assert history == original
        if not eligible:
            assert db.get_session_title("paste") is None
            callback.assert_not_called()
            generate.assert_not_called()
            return
        assert callback.call_args_list[0].args == (preview, "derived")
        expected = "Postgres Pool Exhaustion" if model_upgrade else preview
        assert db.get_session_title("paste") == expected
        if model_upgrade:
            generate.assert_called_once()
            assert generate.call_args.kwargs["title_preview"] == preview
            assert db.get_session_title_source("paste") == "llm"
        else:
            generate.assert_not_called()
            assert db.get_session_title_source("paste") == "derived"
    finally:
        titles.wait_for_title_upgrades(timeout=5)
        db.close()


@pytest.mark.parametrize("opener, preview, expected", [
    ("@file:/pastes/opener.txt", "Postgres connection pool exhaustion", "Postgres connection pool exhaustion"),
    ("@file:/pastes/opener.txt", "   ", None),
    ("[CONTEXT COMPACTION] synthetic handoff", "Postgres connection pool exhaustion", None),
])
def test_direct_instant_title_uses_the_same_preview_eligibility(tmp_path, opener, preview, expected):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="paste", source="telegram")
    try:
        assert titles.apply_instant_title(db, "paste", opener, title_preview=preview) == expected
        assert db.get_session_title("paste") == expected
    finally:
        db.close()
