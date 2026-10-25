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


@pytest.mark.parametrize("style,title,expected", [
    ("verb_first", "Draft reply to Usman", None),
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
