"""A goal_set resume that carries a user_quote: autonomy, curly apostrophes, and the refusal reason.

Field failure (2026-11-23): an agent-paused goal resumed with ``user_quote`` returned
``approval_cards_hidden`` and never said why the quote was refused.
"""
import pytest

import hermes_bootstrap  # noqa: F401  (loaded before the per-test real-home guard)

from hermes_cli import goals, session_controls

# The two quotes from the field failure. Note the curly apostrophe in "week’s".
FIELD_MESSAGE = ("Resume the Agent Components goal. Also send me this week\u2019s Agent Components "
                 "status as a PDF report with a phone link.")
FIELD_QUOTES = [FIELD_MESSAGE, "Resume the Agent Components goal."]


def _home(tmp_path, monkeypatch, config):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    assert db is not None
    db.create_session("work", "telegram", profile_name="default", chat_id="work",
                      chat_type="private", session_key="agent:main:telegram:dm:work")
    db.set_session_title("work", "Agent Components")
    return db


@pytest.fixture
def autonomy_home(tmp_path, monkeypatch):
    db = _home(tmp_path, monkeypatch, "goals:\n  autonomy: true\n  notices: hidden\n")
    yield db
    goals._DB_CACHE.clear()


@pytest.fixture
def hidden_home(tmp_path, monkeypatch):
    """The 2026-11-23 profile: cards hidden, goals.autonomy not on."""
    db = _home(tmp_path, monkeypatch, "goals:\n  autonomy: false\n  notices: hidden\n")
    yield db
    goals._DB_CACHE.clear()


@pytest.fixture
def plain_home(tmp_path, monkeypatch):
    db = _home(tmp_path, monkeypatch, "goals:\n  autonomy: false\n")
    yield db
    goals._DB_CACHE.clear()


def _resume(quote):
    return session_controls.apply_control("goal", "resume", "work", requester_sid="work",
                                          reason="user asked", user_quote=quote)


def _status():
    state = goals.load_goal("work")
    assert state is not None
    return state.status


def _agent_paused():
    mgr = goals.GoalManager("work")
    mgr.set("Agent Components")
    mgr.pause(reason="waiting on a cron run", by="agent")


@pytest.mark.parametrize("quote", FIELD_QUOTES)
def test_quote_on_an_agent_paused_goal_never_hides_behind_approval_under_autonomy(autonomy_home, quote):
    _agent_paused()
    autonomy_home.append_message("work", "user", FIELD_MESSAGE)
    result = _resume(quote)
    assert result["ok"] and result["status"] == "applied", result
    assert result.get("error_code") != "approval_cards_hidden"
    assert result["record"]["authority"]["via"] in {"autonomy", "quote"}
    assert _status() == "active"


@pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
def test_a_possessive_apostrophe_is_not_quoted_data(autonomy_home, apostrophe):
    """A straight and a curly possessive must be judged alike: neither is a quotation mark."""
    title = f"Builder{apostrophe}s"
    autonomy_home.set_session_title("work", title)
    goals.GoalManager("work").set("Watch the build")
    goals.GoalManager("work").pause()  # a user pause: only the user's words lift it
    message = f"Resume the {title} goal."
    autonomy_home.append_message("work", "user", message)
    checked = session_controls.check_user_quote("work", message)
    assert not isinstance(checked, str), checked
    result = _resume(message)
    assert result["ok"] and result["record"]["authority"]["via"] == "quote", result
    assert _status() == "active"


@pytest.mark.parametrize("text", ["Resume the \u2018Agent Components\u2019 goal", "Resume the \u201cgoal\u201d now",
                                  "Resume the goal now\u2019 "])
def test_real_quotation_marks_still_disqualify_a_message(autonomy_home, text):
    goals.GoalManager("work").set("Agent Components")
    autonomy_home.append_message("work", "user", text)
    assert session_controls.check_user_quote("work", text) == "user_quote_qualified"


@pytest.mark.parametrize("quote", FIELD_QUOTES)
def test_field_quotes_are_refused_for_their_real_reason_and_never_apply(hidden_home, quote):
    _agent_paused()
    hidden_home.append_message("work", "user", FIELD_MESSAGE)
    result = _resume(quote)
    # Nothing may apply: the message holds a second instruction, so the quote is not enough.
    assert not result["ok"] and _status() == "paused"
    assert result["error_code"] == "approval_cards_hidden"
    assert result["quote_refused"] == "user_quote_multi_sentence"
    assert "user_quote_multi_sentence" in result["error"]
    assert "own message" in result["error"]
    assert "pass their exact words" not in result["error"]


@pytest.mark.parametrize("typed,quote,reason", [
    ("Please carry on with the work", "Resume the Agent Components goal.", "user_quote_not_found"),
    ("Resume it", "Resume it", "user_quote_too_short"),
    ("Clear the Agent Components goal", "Clear the Agent Components goal", "user_quote_mismatch"),
])
def test_a_refused_quote_reports_its_reason_when_cards_are_hidden(hidden_home, typed, quote, reason):
    _agent_paused()
    hidden_home.append_message("work", "user", typed)
    result = _resume(quote)
    assert not result["ok"] and result["error_code"] == "approval_cards_hidden"
    assert result["quote_refused"] == reason
    assert reason in result["error"]
    assert _status() == "paused"


def test_a_missing_quote_keeps_the_original_hidden_card_guidance(hidden_home):
    _agent_paused()
    result = _resume("")
    assert not result["ok"] and result["error_code"] == "approval_cards_hidden"
    assert "quote_refused" not in result
    assert "pass their exact words as user_quote" in result["error"]


def test_the_quote_must_still_come_from_the_latest_typed_message(hidden_home):
    """Anti-replay: an older message that matched does not authorize a later request."""
    _agent_paused()
    hidden_home.append_message("work", "user", "Resume the Agent Components goal")
    hidden_home.append_message("work", "user", "Thanks, looks good")
    result = _resume("Resume the Agent Components goal")
    assert not result["ok"] and result["quote_refused"] == "user_quote_not_found"
    assert _status() == "paused"


def test_visible_cards_still_fall_back_to_approval_with_the_reason(plain_home):
    _agent_paused()
    plain_home.append_message("work", "user", FIELD_MESSAGE)
    result = _resume(FIELD_QUOTES[1])
    assert result["status"] == "pending" and result["quote_refused"] == "user_quote_multi_sentence"
