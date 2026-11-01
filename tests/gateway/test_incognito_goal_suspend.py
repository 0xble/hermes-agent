"""A goal is suspended while its topic is private and resumes untouched after /incognito off.

While private the judge would send the private reply to the auxiliary goal_judge provider, and
state.db withholds every goal write, so judging, reviving and waking a goal are all skipped.
"""
import json
import logging
import secrets
from unittest.mock import AsyncMock

import pytest

from gateway.incognito import event_scope
from hermes_cli.goals import GoalManager
from tests.gateway import test_incognito_turn_e2e as e2e

harness = e2e.harness
private_route = e2e.private_route


@pytest.fixture
def goal_topic(private_route, monkeypatch):
    runner, store, entry, config, event, db = private_route
    e2e.started(runner, db)
    provider = e2e._ProviderBoundary()
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", provider)
    judged = []

    def judge(call_llm, system_prompt, user_prompt, timeout):
        judged.append(user_prompt)
        return '{"verdict": "continue", "reason": "more work remains"}'

    monkeypatch.setattr("hermes_cli.goals._call_goal_judge_llm", judge)
    GoalManager(session_id=entry.session_id).set("Finish the report")

    async def turn(text, message_id):
        state = runner._session_state(entry.session_key)
        state.persistent.run_generation += 1
        incoming = event(text)
        incoming.message_id = message_id
        with event_scope(runner, incoming):
            reply = await runner._handle_message_with_agent(
                incoming, incoming.source, entry.session_key, state.persistent.run_generation)
            await runner._run_post_turn_hooks(agent_result=reply, source=incoming.source, is_internal=False,
                                              event=incoming)
        return reply

    return runner, entry, event, provider, judged, turn


def goal_row(sid):
    return json.loads(GoalManager(session_id=sid).state.to_json())


@pytest.mark.asyncio
async def test_private_turns_skip_the_judge_and_leave_the_goal_untouched(goal_topic, caplog):
    runner, entry, event, provider, judged, turn = goal_topic
    sid = entry.session_id
    before = goal_row(sid)
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    canary = "canary-" + secrets.token_hex(12)
    caplog.set_level(logging.INFO)
    provider.replies = [f"OK {canary}"]
    assert await turn(f"{canary}: reply with just OK", "private-1") == f"OK {canary}"
    assert judged == [], "the goal judge saw a private reply"
    assert not [r for r in caplog.records if r.name == "hermes_cli.goals" and r.levelno >= logging.WARNING]
    assert goal_row(sid) == before

    assert "OFF" in await e2e.incognito(runner, event, "/incognito off")
    provider.replies = ["Public progress"]
    assert await turn("Public follow-up", "public-1") == "Public progress"
    assert len(judged) == 1 and "Public progress" in judged[0] and canary not in judged[0]
    assert goal_row(sid)["last_reason"] == "more work remains"


@pytest.mark.asyncio
async def test_blocked_goal_is_revived_only_by_a_public_user_turn(goal_topic, caplog):
    runner, entry, event, provider, judged, turn = goal_topic
    sid = entry.session_id

    def block(state):
        state.status, state.last_verdict = "paused", "blocked"
        state.paused_reason = "judge blocked: needs the user's numbers"

    GoalManager(session_id=sid)._mutate_current(block)
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    provider.replies = ["Private answer"]
    caplog.set_level(logging.INFO)
    await turn("Here are private numbers", "private-2")
    assert goal_row(sid)["status"] == "paused"
    # Not attempted, rather than attempted and withheld by state.db.
    assert not [r for r in caplog.records if r.name == "hermes_cli.goals" and r.levelno >= logging.WARNING]

    assert "OFF" in await e2e.incognito(runner, event, "/incognito off")
    provider.replies = ["Public answer"] * 2
    await turn("Here are the numbers", "public-2")
    assert goal_row(sid)["status"] == "active"


@pytest.mark.asyncio
async def test_parked_goal_is_not_woken_into_a_private_topic(goal_topic, monkeypatch):
    runner, entry, event, _provider, _judged, _turn = goal_topic
    adapter = next(iter(runner.adapters.values()))
    adapter._message_handler = AsyncMock()
    checked = []
    monkeypatch.setattr(GoalManager, "lifted_barrier_prompt", lambda self: checked.append(self.session_id))
    assert "private-model" in await e2e.incognito(runner, event, "/incognito on")
    await runner._goal_wakeup_fire_one(entry.session_id)
    assert checked == []
    assert "OFF" in await e2e.incognito(runner, event, "/incognito off")
    await runner._goal_wakeup_fire_one(entry.session_id)
    assert checked == [entry.session_id]

