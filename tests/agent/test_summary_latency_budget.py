"""Offline budget, focus admission, and synthetic checkpoint quality contracts."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent.context_compressor as cc
from agent.compact_context import request_compaction
from agent.model_metadata import estimate_messages_tokens_rough


def _compressor():
    comp = cc.ContextCompressor(model="test/model", quiet_mode=True)
    comp.context_length = 272_000
    comp.max_summary_tokens = 10_000
    return comp


def test_summary_budget_scales_with_bounded_content_and_lowered_cap():
    comp = _compressor()
    small = "[USER]: Continue the open task."
    large = "repeated diagnostic output " * 6_000
    assert comp._compute_summary_budget(small) == 2_000
    assert 2_000 < comp._compute_summary_budget(large) < 7_000
    comp.max_summary_tokens = 800
    assert comp._compute_summary_budget(small) == 800


def test_lean_appendices_do_not_count_as_model_budget_overrun(caplog):
    comp = _compressor()
    turns = [{"role": "user", "content": f"Turn {i}: " + "ordinary user context " * 52} for i in range(30)]
    body = "## Critical Context\nContinue the open task."
    telemetry = comp._begin_compression_telemetry(current_tokens=40_000)
    with patch.object(comp, "_call_summary_llm", return_value=body):
        summary = comp._generate_summary(turns)
    assert summary and "User Messages" in summary
    assert estimate_messages_tokens_rough([{"role": "assistant", "content": summary}]) > telemetry["summary_budget_tokens"]
    assert telemetry["summary_output_tokens"] == estimate_messages_tokens_rough([{"role": "assistant", "content": body}])
    assert telemetry["summary_budget_overrun_tokens"] == 0
    assert "summary budget exceeded" not in caplog.text


@pytest.mark.parametrize("tail_mode", ["lean", "legacy"])
def test_generation_serializes_once_and_budgets_actual_prompt_content(tail_mode):
    comp = _compressor()
    comp.tail_mode = tail_mode
    comp._previous_summary = "Previous durable facts."
    turns = [{"role": "user", "content": "Continue the open task."}]
    turns += [{"role": "tool", "content": f"Observation {i}: " + "diagnostic output " * 400} for i in range(80)]
    with (
        patch.object(comp, "_serialize_records_for_summary", wraps=comp._serialize_records_for_summary) as serialize,
        patch.object(comp, "_compute_summary_budget", wraps=comp._compute_summary_budget) as budget,
        patch.object(comp, "_build_summary_prompt", wraps=comp._build_summary_prompt) as build_prompt,
        patch.object(comp, "_call_summary_llm", return_value="## Critical Context\nNo open task."),
    ):
        assert comp._generate_summary(turns)
    assert serialize.call_count == 1
    content = build_prompt.call_args.args[0]
    budget.assert_called_once_with(content)
    content_tokens = estimate_messages_tokens_rough([{"role": "user", "content": "Previous durable facts." + content}])
    assert build_prompt.call_args.args[1] == max(2_000, min(10_000, int(content_tokens * cc._SUMMARY_RATIO)))


def test_small_context_default_keeps_floor_but_lowered_manual_cap_overrides_it():
    comp = _compressor()
    comp.context_length = 32_000
    comp.max_summary_tokens = 1_600
    turns = [{"role": "user", "content": "Continue the open task."}]
    telemetry = comp._begin_compression_telemetry(current_tokens=20_000)
    with patch.object(comp, "_call_summary_llm", return_value="## Critical Context\nContinue the open task."):
        assert comp._generate_summary(turns)
        assert telemetry["summary_budget_tokens"] == 2_000
        comp.max_summary_tokens = 800
        assert comp._generate_summary(turns)
        assert telemetry["summary_budget_tokens"] == 800


def test_focus_admission_never_silently_drops_identifiers():
    agent = SimpleNamespace(context_compressor=SimpleNamespace(threshold_tokens=1000, last_prompt_tokens=500))
    focus = "x" * 1490 + " /a/123abc"
    assert json.loads(request_compaction(agent, {"reason": "phase done", "focus": focus}))["accepted"]
    assert agent._pending_agent_compaction["focus"] == focus
    agent = SimpleNamespace(context_compressor=SimpleNamespace(threshold_tokens=1000, last_prompt_tokens=500))
    result = json.loads(request_compaction(agent, {"reason": "phase done", "focus": "x" * 1501}))
    assert not result["accepted"]
    assert "1500" in result["reason"] and "identifiers" in result["reason"]
    assert not hasattr(agent, "_pending_agent_compaction")


def test_offline_checkpoint_quality_and_aux_usage_overrun(caplog):
    # Real-shaped but wholly synthetic: repeated observations around sparse durable facts.
    facts = [
        "Issue #314, task FIX-72, tool call call_19.",
        "SHA 0123456789abcdef0123456789abcdef01234567.",
        "Path /repo/agent/context_compressor.py:828.",
        'User quote: "Do not deploy or reset the session."',
        "Decision: preserve the original identity because recovery must be lossless.",
        "Open task: run the exact-head gate, then await approval.",
    ]
    turns = []
    for fact in facts:
        turns.extend([{"role": "user", "content": fact}, {"role": "tool", "content": "unchanged diagnostic observation\n" * 180}])
    comp = _compressor()
    telemetry = comp._begin_compression_telemetry(current_tokens=40_000)
    captured = {}
    initial_budget = comp._compute_summary_budget(comp._sample_summary_records(comp._serialize_records_for_summary(turns))[0])

    def fake_llm(**kwargs):
        captured.update(kwargs)
        prompt = kwargs["messages"][0]["content"]
        for fact in facts:
            assert fact in prompt
        # Deterministic extractive fake checks the integration, not model compliance.
        body = "## Critical Context\n" + "\n".join(facts)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=body), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=12_345, completion_tokens=7_654),
        )

    with patch.object(cc, "call_llm", side_effect=fake_llm):
        summary = comp._generate_summary(turns)
    assert summary
    for fact in facts:
        assert fact in summary
    assert "max_tokens" not in captured
    assert telemetry["aux_input_tokens"] == 12_345
    assert telemetry["aux_output_tokens"] == 7_654
    assert telemetry["summary_budget_tokens"] == initial_budget
    assert telemetry["summary_output_tokens"] <= telemetry["summary_budget_tokens"]
    assert telemetry["summary_budget_overrun_tokens"] == 0
    assert telemetry["aux_call_duration_ms"] >= 0
    # Both the previous 20%-raw budget and the new budget accommodate all required facts.
    fact_tokens = estimate_messages_tokens_rough([{"role": "assistant", "content": "\n".join(facts)}])
    old_budget = max(2_000, min(10_000, int(estimate_messages_tokens_rough(turns) * .20)))
    assert fact_tokens < telemetry["summary_budget_tokens"] <= old_budget
    old_comp = _compressor()
    with patch.object(old_comp, "_compute_summary_budget", return_value=old_budget), patch.object(cc, "call_llm", side_effect=fake_llm):
        old_summary = old_comp._generate_summary(turns)
    assert old_summary
    for fact in facts:
        assert fact in old_summary
    # An overrun is measured, never clipped or converted into another expensive call.
    comp._record_summary_output(summary + " redundant observation" * 2500, initial_budget)
    assert telemetry["summary_budget_overrun_tokens"] > 0
    assert "summary budget exceeded" in caplog.text
