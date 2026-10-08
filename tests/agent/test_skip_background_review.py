"""Tests for the skip_background_review constructor flag.

Verifies that AIAgent can be instructed to skip the end-of-turn
_spawn_background_review fork (~30K tokens / event), which is essential
on cron sessions that have no human-in-the-loop value from skill/memory
review forks.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from run_agent import AIAgent
from agent.turn_finalizer import finalize_turn


def _make_agent(skip_background_review: bool = False) -> AIAgent:
    """Construct a minimally-configured AIAgent for unit testing."""
    return AIAgent(
        model="openai/gpt-4o-mini",
        provider="openrouter",
        api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=skip_background_review,
        platform="cli",
    )


def _stub_agent_for_finalize(agent: AIAgent) -> None:
    """Stub the heavy finalizer dependencies to isolate the review gate."""
    agent._spawn_background_review = MagicMock()
    agent._save_trajectory = MagicMock()
    agent._cleanup_task_resources = MagicMock()
    agent._persist_session = MagicMock()
    agent._session_messages = []
    agent._file_mutation_verifier_enabled = lambda: False
    agent.clear_interrupt = MagicMock()
    agent._stream_callback = None
    agent._sync_external_memory_for_turn = MagicMock()
    agent._skill_nudge_interval = 10
    agent._iters_since_skill = 20  # exceeds nudge interval → _should_review_skills = True
    agent.valid_tool_names = {"skill_manage"}
    agent.iteration_budget = MagicMock()
    agent.iteration_budget.remaining = 100
    agent.iteration_budget.used = 5
    agent.iteration_budget.max_total = 100
    agent.max_iterations = 50
    agent._emit_status = MagicMock()
    agent._safe_print = MagicMock()
    agent._apply_persist_user_message_override = MagicMock()
    agent.context_compressor = None
    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    agent.model = "test-model"
    agent.session_id = "test-session"
    agent.quiet_mode = True
    agent._turn_failed_file_mutations = {}
    agent._db_flush_scan_prefix = None


def _run_finalize(agent: AIAgent, user_message="test", *, review_memory=True) -> None:
    """Call finalize_turn with conditions that would trigger background review."""
    finalize_turn(
        agent,
        final_response="ok",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "assistant", "content": "ok"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="test-turn",
        user_message=user_message,
        original_user_message=user_message,
        _should_review_memory=review_memory,
        _turn_exit_reason="text_response(1)",
    )


def _finish_review_turn(
    agent,
    path,
    monkeypatch,
    user_message,
    *,
    iterations,
    display_kind=None,
    review_memory=False,
):
    agent._turn_display_kind = display_kind
    if path == "chat":
        agent._iters_since_skill += iterations
        _run_finalize(agent, user_message, review_memory=review_memory)
    else:
        from agent import codex_runtime

        monkeypatch.setattr(
            codex_runtime, "_record_codex_app_server_compaction", lambda *a: None
        )
        monkeypatch.setattr(
            codex_runtime, "_record_codex_app_server_usage", lambda *a, **kw: {}
        )
        turn = SimpleNamespace(
            tool_iterations=iterations, final_text="ok", interrupted=False, error=None
        )
        codex_runtime._finish_codex_turn(
            agent,
            turn,
            [],
            original_user_message=user_message,
            should_review_memory=review_memory,
        )


@pytest.mark.parametrize("path", ["chat", "codex"])
def test_skill_review_requires_a_human_turn_since_the_previous_review(
    path, monkeypatch
):
    from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE

    generated = CONTINUATION_PROMPT_TEMPLATE.format(goal="Finish the task")
    for display_kind in (None, "internal_notification", "async_delegation_complete"):
        agent = _make_agent()
        _stub_agent_for_finalize(agent)
        agent._iters_since_skill = 0

        # Entirely generated turns cross the threshold without providing new user evidence.
        for _ in range(2):
            _finish_review_turn(
                agent,
                path,
                monkeypatch,
                generated,
                iterations=5,
                display_kind=display_kind,
            )
        agent._spawn_background_review.assert_not_called()
        assert agent._iters_since_skill == 10

        # Memory reviews remain eligible even on synthetic-only windows.
        _finish_review_turn(
            agent,
            path,
            monkeypatch,
            generated,
            iterations=0,
            display_kind=display_kind,
            review_memory=True,
        )
        assert agent._spawn_background_review.call_args.kwargs["review_memory"] is True
        assert agent._spawn_background_review.call_args.kwargs["review_skills"] is False
        agent._spawn_background_review.reset_mock()

        _finish_review_turn(
            agent,
            path,
            monkeypatch,
            "Please preserve my naming convention",
            iterations=0,
        )
        agent._spawn_background_review.assert_called_once()
        assert agent._spawn_background_review.call_args.kwargs["review_skills"] is True
        assert agent._iters_since_skill == 0
        agent._spawn_background_review.reset_mock()

        # Consuming the review must also consume the human evidence, not just the counter.
        _finish_review_turn(
            agent,
            path,
            monkeypatch,
            generated,
            iterations=10,
            display_kind=display_kind,
        )
        agent._spawn_background_review.assert_not_called()
        assert agent._iters_since_skill == 10


def test_finalize_turn_skips_review_when_flag_set() -> None:
    """finalize_turn must NOT call _spawn_background_review when skip_background_review=True.

    Exercises the actual finalizer call path (not a duplicated guard expression)
    so it catches divergence between the production guard and the test.
    """
    agent = _make_agent(skip_background_review=True)
    _stub_agent_for_finalize(agent)
    _run_finalize(agent)
    agent._spawn_background_review.assert_not_called()


def test_finalize_turn_fires_review_when_flag_unset() -> None:
    """Counterpart: with the flag off, finalize_turn DOES call _spawn_background_review."""
    agent = _make_agent(skip_background_review=False)
    _stub_agent_for_finalize(agent)
    _run_finalize(agent)
    agent._spawn_background_review.assert_called_once()


def test_persistence_failure_error_fallback_is_pinned_and_leaves_final_response_empty(monkeypatch, tmp_path) -> None:
    """With no model text, result["error"] carries a profile-pinned `hermes doctor`, while the
    memory sync and the background-review gate still see the turn as having produced nothing."""
    from hermes_constants import profile_cli_selector

    # A named profile home must exist before an agent is built inside it: setup_logging() now
    # opens agent.log under the ACTIVE home and refuses to materialize a missing profile.
    profile_home = tmp_path / ".hermes" / "profiles" / "research"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    selector = profile_cli_selector()
    assert selector.strip()
    agent = _make_agent()
    _stub_agent_for_finalize(agent)
    # Force the fallback: the explainer normally supplies the text, so an empty explainer is
    # the only way the hardcoded copy reaches the user.
    monkeypatch.setattr(
        AIAgent, "_format_turn_completion_explanation", staticmethod(lambda *a, **k: "")
    )
    result = finalize_turn(
        agent,
        final_response="",
        api_call_count=1,
        interrupted=False,
        failed=True,
        messages=[{"role": "user", "content": "hi"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="test-turn",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=True,
        _turn_exit_reason="session_persistence_failed",
    )
    assert f"`hermes {selector}doctor`" in result["error"]
    assert agent._sync_external_memory_for_turn.call_args.kwargs["final_response"] == ""
    agent._spawn_background_review.assert_not_called()
