import pytest

from hermes_cli.reasoning_turn import (
    ReasoningTurnError,
    parse_reasoning_turn,
)


def test_parse_reasoning_turn_preserves_multiline_prompt():
    request = parse_reasoning_turn("high Fix the parser\nand keep the newline")

    assert request is not None
    assert request.effort == "high"
    assert request.reasoning_config == {"enabled": True, "effort": "high"}
    assert request.prompt == "Fix the parser\nand keep the newline"
    assert request.notice == "🧠 Reasoning: High for this turn."


@pytest.mark.parametrize("raw", ["", "high", " high  ", "show", "reset"])
def test_parse_reasoning_turn_abstains_without_prompt(raw):
    assert parse_reasoning_turn(raw) is None


def test_parse_reasoning_turn_rejects_disabled_effort():
    with pytest.raises(ReasoningTurnError, match="disabled"):
        parse_reasoning_turn("none answer this")


def test_parse_reasoning_turn_rejects_global_scope():
    with pytest.raises(ReasoningTurnError, match="--global"):
        parse_reasoning_turn("high --global answer this")


def test_parse_reasoning_turn_does_not_claim_unknown_subcommand():
    assert parse_reasoning_turn("turbo answer this") is None
