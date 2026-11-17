"""Typed session identity lookup tests."""

import json

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def test_lookup_sessions_matches_title_topic_and_slack_route(db):
    db.create_session(
        "lookup-slack", "slack", session_key="agent:main:slack:channel:C123:thread:171.42",
        chat_id="C123", chat_type="channel", thread_id="171.42", display_name="Incident thread",
    )
    db.set_session_title("lookup-slack", "PartyDAO relay delivery")
    db.create_session(
        "lookup-telegram", "telegram", session_key="agent:main:telegram:group:-99:topic:7",
        chat_id="-99", chat_type="group", thread_id="7", display_name="Review topic",
    )
    db.set_session_title("lookup-telegram", "Everguide Review")

    assert [r["session_id"] for r in db.lookup_sessions(title="partydao")] == ["lookup-slack"]
    assert [r["session_id"] for r in db.lookup_sessions(topic_name="Review topic")] == ["lookup-telegram"]
    assert [r["session_id"] for r in db.lookup_sessions(
        platform="slack", chat_id="C123", thread_id="171.42"
    )] == ["lookup-slack"]
    assert db.lookup_sessions(platform="slack", chat_id="C123", thread_id="wrong") == []


def test_lookup_sessions_tool_resolves_title_and_slack_route(db):
    db.create_session(
        "slack-route", source="slack", session_key="agent:main:slack:C123:171.42",
        chat_id="C123", chat_type="channel", thread_id="171.42", display_name="Incident topic",
    )
    db.set_session_title("slack-route", "PartyDAO relay delivery")

    by_title = json.loads(session_search(lookup={"title": "partydao"}, db=db))
    assert by_title["success"] and by_title["mode"] == "lookup"
    assert by_title["results"][0]["session_id"] == "slack-route"

    by_route = json.loads(session_search(
        lookup={"platform": "slack", "chat_id": "C123", "thread_id": "171.42"}, db=db,
    ))
    assert by_route["success"] and by_route["results"][0]["session_id"] == "slack-route"


def test_lookup_sessions_requires_a_typed_selector(db):
    with pytest.raises(ValueError, match="lookup requires"):
        db.lookup_sessions()


def test_typed_lookup_rejects_empty_selector(db):
    result = json.loads(session_search(lookup={}, db=db))
    assert result["success"] is False and "lookup requires" in result["error"]
