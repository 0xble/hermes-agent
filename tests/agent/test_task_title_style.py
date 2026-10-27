"""Task title opt-in preserves the shared noun title default."""
import json
from types import SimpleNamespace

import pytest

from agent import title_generator as titles


@pytest.fixture(autouse=True)
def title_config(monkeypatch):
    monkeypatch.setattr(titles, "_title_config", dict)


def test_prompt_style_selection():
    noun = titles._title_prompt(language="")
    assert "Use a noun phrase, never an imperative" in noun
    assert noun == titles._title_prompt(language="", title_style="noun")
    task = titles._title_prompt(language="", title_style="verb_first")
    assert "Start with a concrete action verb" in task
    assert "Sentence case" in task
    assert "3 to 7 words" in task
    assert "Privacy-safe" in task
    assert "Name one outcome only" in task
    assert "Use a noun phrase" not in task
    assert f"Aim for {titles.TASK_TITLE_TARGET_CHARS} characters or fewer" in task
    assert "never cut a word short" in task
    assert "characters or fewer" not in noun
    for _bad, good in titles._TASK_PROMPT_PAIRS:
        assert len(good) <= titles.TASK_TITLE_TARGET_CHARS, good


@pytest.mark.parametrize("style,title,expected", [
    ("verb_first", "Draft reply to Usman", "Draft reply to Usman"),
    ("verb_first", "Auto-rename Slack threads", "Auto-rename Slack threads"),
    ("verb_first", "Summarize Science memo", "Summarize Science memo"),
    ("verb_first", "Reply to Usman discussion", None),
    # The character target is guidance only: a longer title that fits the hard ceiling is kept.
    ("verb_first", "Apply Max Hodak company-building lessons", "Apply Max Hodak company-building lessons"),
    ("noun", "Draft reply to Usman", "Draft reply to Usman"),
    ("verb_first", "Friendly greeting", "Friendly greeting"),
    ("verb_first", "Review account renewal options", "Review account renewal options"),
    ("verb_first", "Review " + "x" * 80, None),
])
def test_style_specific_generation_guards(monkeypatch, style, title, expected):
    prompts = []
    def model(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"title": title})))])
    monkeypatch.setattr(titles, "call_llm", model)
    assert titles.generate_title("Help review account renewal options", title_style=style) == expected
    assert ("Start with a concrete action verb" in prompts[0]) == (style == "verb_first")


def test_background_pipeline_carries_style(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "titles.db")
    db.create_session("task-style", source="slack", model="test")
    observed = []
    def generate(message, **kwargs):
        observed.append(kwargs["title_style"])
        return "Review account renewal options"
    monkeypatch.setattr(titles, "generate_title", generate)
    monkeypatch.setattr(titles, "_title_link_context", lambda *_args: None)
    monkeypatch.setattr(titles, "title_upgrade_must_wait_for_turn", lambda _: True)
    thread = titles.maybe_auto_title(db, "task-style", "Review account renewal options", title_style="verb_first")
    assert observed == []
    assert thread is not None
    titles.start_title_upgrade(thread)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert observed == ["verb_first"]
    assert db.get_session_title("task-style") == "Review account renewal options"
    db.close()


@pytest.mark.parametrize("style", ["verb_first", "noun"])
def test_turn_start_and_completion_retry_preserve_style(tmp_path, monkeypatch, style):
    from unittest.mock import Mock
    from agent.turn_context import _maybe_title_session_at_turn_start, start_deferred_title_upgrade
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "turn-titles.db")
    db.create_session("task-turn", source="slack", model="test")
    history = [{"role": "user", "content": "Review account renewal options"}]
    agent = SimpleNamespace(_session_db=db, session_id="task-turn", _session_db_created=True,
                            platform="slack", _title_style=style)
    generate = Mock(side_effect=[None, "Review account renewal options"])
    monkeypatch.setattr(titles, "generate_title", generate)
    monkeypatch.setattr(titles, "_title_link_context", lambda *_args: None)
    monkeypatch.setattr(titles, "title_upgrade_must_wait_for_turn", lambda *_args: False)
    try:
        _maybe_title_session_at_turn_start(agent, history)
        titles.wait_for_title_upgrades(timeout=5)
        assert generate.call_count == 1
        # The noun default is omitted so ordinary sessions keep their unchanged call shape.
        assert generate.call_args.kwargs.get("title_style", "noun") == style
        history.append({"role": "assistant", "content": "The renewal options are ready to compare."})
        start_deferred_title_upgrade(agent)
        titles.wait_for_title_upgrades(timeout=5)
        assert generate.call_count == 2
        assert all(call.kwargs.get("title_style", "noun") == style for call in generate.call_args_list)
        assert generate.call_args.kwargs["reply_excerpt"] == history[-1]["content"]
        assert db.get_session_title("task-turn") == "Review account renewal options"
    finally:
        db.close()
