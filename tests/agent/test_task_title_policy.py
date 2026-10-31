"""Platform task titles must not inherit a conflicting global noun convention."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import title_generator as titles
from hermes_constants import get_hermes_home


@pytest.mark.parametrize("style", ["noun", "verb_first"])
def test_configured_noun_convention_respects_selected_style(monkeypatch, style):
    instructions = "Use a concrete noun phrase, never a command. Prefix with Archive."
    config = {"auxiliary": {"title_generation": {
        "instructions": instructions,
        "min_words": 2,
        "max_words": 4,
        "case_style": "title_case",
        "language": "English",
        "name_aliases": {"1password": "1Password"},
    }}}
    (Path(get_hermes_home()) / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    prompts = []

    def model(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        title = "Explain 1password workspace" if style == "verb_first" else "Archive 1password Workspace"
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"title": title})))])

    monkeypatch.setattr(titles, "call_llm", model)
    expected = "Explain 1Password workspace" if style == "verb_first" else "Archive 1Password Workspace"
    assert titles.generate_title("Why does 1password show the wrong workspace?", title_style=style) == expected
    assert len(prompts) == 1
    prompt = prompts[0]
    if style == "verb_first":
        assert instructions not in prompt
        assert "Start with a concrete action verb" in prompt
        assert "Sentence case" in prompt
        assert "Title case: capitalize the principal words" not in prompt
    else:
        assert instructions in prompt
        assert "Use a noun phrase, never an imperative" in prompt
        assert "Title case: capitalize the principal words" in prompt
    assert "2 to 4 words" in prompt
    assert "English" in prompt


def test_task_title_abstention_retries_after_substantive_followup(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    responses = iter([{"abstain": True}, {"title": "Explain Slack app label"}])
    observed = []
    callbacks = []

    def model(**kwargs):
        observed.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(next(responses))))])

    monkeypatch.setattr(titles, "call_llm", model)
    monkeypatch.setattr(titles, "_title_link_context", lambda *_args: None)
    monkeypatch.setattr(titles, "title_upgrade_must_wait_for_turn", lambda *_args: False)
    db = SessionDB(tmp_path / "titles.db")
    db.create_session("greeting-followup", source="slack", model="test")
    try:
        for message in ("Hello?", "Why is the Slack agent marked App instead of Agent?"):
            thread = titles.maybe_auto_title(
                db, "greeting-followup", message, title_style="verb_first",
                title_callback=lambda title, source, **_kwargs: callbacks.append((title, source)),
            )
            assert thread is not None
            titles.start_title_upgrade(thread)
            thread.join(timeout=5)
            assert not thread.is_alive()
            if message == "Hello?":
                assert db.get_session_title("greeting-followup") in {None, "Hello?"}
                assert not any(source == "llm" for _title, source in callbacks)
        assert db.get_session_title("greeting-followup") == "Explain Slack app label"
        assert len(observed) == 2
        assert all("Start with a concrete action verb" in prompt for prompt in observed)
    finally:
        db.close()
