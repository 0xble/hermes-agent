from types import SimpleNamespace

from agent.adaptive_reasoning import (
    POLICY_VERSION,
    begin_adaptive_reasoning_turn,
    parse_adaptive_reasoning_config,
    select_adaptive_reasoning,
)
from agent.reasoning_context import (
    begin_turn_reasoning,
    get_turn_reasoning_config,
    reset_turn_reasoning,
    set_turn_reasoning_config,
)


def _agent(*, baseline="medium", config=None, user_override=False):
    notices = []
    return SimpleNamespace(
        reasoning_config={"enabled": True, "effort": baseline},
        adaptive_reasoning=parse_adaptive_reasoning_config(
            config if config is not None else {"enabled": True}
        ),
        reasoning_user_override=user_override,
        notice_callback=notices.append,
        platform="cli",
        notices=notices,
    )


def test_config_is_opt_in_and_escalation_only_by_default():
    assert parse_adaptive_reasoning_config(None) is None
    assert parse_adaptive_reasoning_config({"enabled": False}) is None
    assert parse_adaptive_reasoning_config({"enabled": True}) == {
        "enabled": True,
        "max_effort": "high",
    }


def test_decision_is_deterministic_explainable_and_versioned():
    text = "Debug the production crash. error: connection refused"
    first = select_adaptive_reasoning(text, "medium", {"enabled": True})
    second = select_adaptive_reasoning(text, "medium", {"enabled": True})

    assert first == second
    assert first.selected_effort == "high"
    assert first.score >= 3
    assert "debugging" in first.reason_codes
    assert first.policy_version == POLICY_VERSION
    assert first.applied is True


def test_uncertain_message_abstains_to_baseline():
    decision = select_adaptive_reasoning(
        "Can you take a look at this?", "medium", {"enabled": True}
    )

    assert decision.selected_effort == "medium"
    assert decision.applied is False
    assert decision.reason_codes == ("abstain",)


def test_default_policy_never_downshifts_or_escalates_simple_work():
    for baseline in ("minimal", "medium"):
        decision = select_adaptive_reasoning(
            "thanks", baseline, {"enabled": True}
        )

        assert decision.selected_effort == baseline
        assert decision.applied is False
        assert "simple" in decision.reason_codes


def test_explicit_minimum_opts_into_deterministic_downshift():
    decision = select_adaptive_reasoning(
        "thanks", "medium", {"enabled": True, "min_effort": "low"}
    )

    assert decision.selected_effort == "low"
    assert decision.applied is True


def test_policy_never_selects_none():
    for text in ("thanks", "Debug the crash", "Design the security architecture"):
        decision = select_adaptive_reasoning(
            text,
            "medium",
            {"enabled": True, "min_effort": "none", "max_effort": "none"},
        )
        assert decision.selected_effort != "none"


def test_xhigh_requires_corroborating_complexity_signal():
    high = select_adaptive_reasoning(
        "Perform a security review of this endpoint", "medium", {"enabled": True}
    )
    xhigh = select_adaptive_reasoning(
        "Design the cross-component security architecture for the production migration",
        "medium",
        {"enabled": True, "max_effort": "xhigh"},
    )

    assert high.selected_effort == "high"
    assert xhigh.selected_effort == "xhigh"


def test_begin_applies_task_local_override_and_emits_notice():
    agent = _agent()
    token = begin_turn_reasoning(agent)
    try:
        decision = begin_adaptive_reasoning_turn(
            agent, "Debug the production crash. error: connection refused"
        )
        assert decision is not None and decision.applied
        assert get_turn_reasoning_config(agent)["effort"] == "high"
        assert agent.reasoning_config["effort"] == "medium"
        assert len(agent.notices) == 1
        assert (
            agent.notices[0].text
            == "🧠 Adaptive reasoning: Medium → High · complex debugging task"
        )
    finally:
        reset_turn_reasoning(token)


def test_adaptive_uses_medium_when_reasoning_uses_provider_default():
    agent = SimpleNamespace(
        adaptive_reasoning={"enabled": True},
        reasoning_config=None,
        reasoning_user_override=False,
        notices=[],
    )
    agent.notice_callback = agent.notices.append

    token = begin_turn_reasoning(agent)
    try:
        decision = begin_adaptive_reasoning_turn(
            agent, "Debug this failing parser and trace the exception"
        )
        assert decision is not None and decision.applied is True
        active = get_turn_reasoning_config(agent)
        assert active is not None and active["effort"] == "high"
    finally:
        reset_turn_reasoning(token)


def test_explicit_turn_override_beats_adaptive():
    agent = _agent()
    token = begin_turn_reasoning(agent)
    try:
        assert set_turn_reasoning_config(
            agent,
            {"enabled": True, "effort": "low"},
            source="explicit",
        )
        decision = begin_adaptive_reasoning_turn(
            agent, "Debug the production crash. error: connection refused"
        )
        assert decision is not None and decision.applied is False
        assert decision.reason_codes == ("explicit-override",)
        assert get_turn_reasoning_config(agent)["effort"] == "low"
        assert agent.notices == []
    finally:
        reset_turn_reasoning(token)


def test_session_user_override_beats_adaptive():
    agent = _agent(user_override=True)
    token = begin_turn_reasoning(agent)
    try:
        decision = begin_adaptive_reasoning_turn(agent, "Implement a parser with tests")
        assert decision is not None and decision.applied is False
        assert decision.reason_codes == ("explicit-override",)
        assert get_turn_reasoning_config(agent) is None
    finally:
        reset_turn_reasoning(token)
