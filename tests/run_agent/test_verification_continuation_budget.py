"""End-to-end regression coverage for verification budget exhaustion (#61631, #65919 §7)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent, _is_ephemeral_scaffolding
from agent.turn_finalizer import (
    _collapse_verification_candidates,
    _compose_verification_receipt_with_answer,
)


def _response(content="composed report"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="verify-budget-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def _assert_pending_response_survives(agent, result):
    assert result["final_response"] == "composed report"
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["completed"] is False
    assert agent._handle_max_iterations.call_count == 0
    # The nudge is stripped by _drop_verification_continuation_scaffolding,
    # so the role sequence is [user, assistant] — the candidate is the
    # tail and matches final_response so it is not duplicated. (#65919 §7)
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
    ]


def test_verification_receipt_cannot_replace_substantive_answer():
    pending = "Implemented the Exa usage estimator and documented its evidence contract."
    receipt = "Fresh verification from this turn passes:\n\n`pnpm run lint`"

    result = _compose_verification_receipt_with_answer(pending, receipt)

    assert result == (
        "Implemented the Exa usage estimator and documented its evidence contract.\n\n"
        "## Verification\n\n"
        "Fresh verification from this turn passes:\n\n`pnpm run lint`"
    )


def test_punctuation_free_success_receipt_preserves_pending_answer():
    pending = "Implemented the requested change."
    receipt = "Fresh verification from this turn passes"

    assert _compose_verification_receipt_with_answer(
        pending,
        receipt,
    ) == f"{pending}\n\n## Verification\n\n{receipt}"


@pytest.mark.parametrize(
    "receipt",
    [
        "Fresh verification from this turn passes: pytest -q",
        "Fresh verification passes: ruff check .\npytest -q",
    ],
)
def test_plain_text_success_receipts_preserve_pending_answer(receipt):
    pending = "Implemented the requested change."
    assert _compose_verification_receipt_with_answer(
        pending,
        receipt,
    ) == f"{pending}\n\n## Verification\n\n{receipt}"


@pytest.mark.parametrize(
    "receipt",
    [
        "I couldn't provide fresh verification evidence for this edit",
        "I could not provide fresh verification evidence for the edit.",
    ],
)
def test_equivalent_failure_receipts_preserve_pending_answer(receipt):
    pending = "Implemented the requested change."
    assert _compose_verification_receipt_with_answer(
        pending,
        receipt,
    ) == f"{pending}\n\n## Verification\n\n{receipt}"


def test_punctuation_free_failure_receipt_preserves_pending_answer():
    pending = "Implemented the requested change."
    receipt = "I cannot provide fresh verification evidence for this edit"

    assert _compose_verification_receipt_with_answer(
        pending,
        receipt,
    ) == f"{pending}\n\n## Verification\n\n{receipt}"


def test_substantive_verification_heading_is_never_parsed_as_a_receipt():
    pending = "Verification report:\n\nThe migration preserved every customer row."
    receipt = "I cannot provide fresh verification evidence for that edit."

    result = _compose_verification_receipt_with_answer(pending, receipt)

    assert result == f"{pending}\n\n## Verification\n\n{receipt}"


def test_authored_verification_section_is_preserved_verbatim():
    pending = "Analysis\n\n## Verification\n\nThe authored evidence narrative."
    receipt = "Fresh verification from this turn passes."

    result = _compose_verification_receipt_with_answer(pending, receipt)

    assert result == f"{pending}\n\n## Verification\n\n{receipt}"


def test_complete_later_answer_still_replaces_pending_answer():
    result = _compose_verification_receipt_with_answer(
        "The initial answer.",
        "The complete verified answer.",
    )

    assert result == "The complete verified answer."


@pytest.mark.parametrize(
    "complete_answer",
    [
        (
            "I cannot provide fresh verification evidence for the original edit, "
            "so I reverted it and implemented the corrected solution."
        ),
        "Verification report: the corrected implementation is complete.",
        (
            "Fresh verification from this turn passes. I found the original fix "
            "was wrong and replaced it with the corrected solution."
        ),
    ],
)
def test_substantive_verification_response_replaces_pending_answer(complete_answer):
    assert _compose_verification_receipt_with_answer(
        "The obsolete answer.",
        complete_answer,
    ) == complete_answer


def test_provisional_verification_candidates_remain_crash_durable():
    assert not _is_ephemeral_scaffolding(
        {
            "role": "assistant",
            "content": "candidate",
            "_verification_candidate": True,
        }
    )


def test_provisional_verification_candidate_is_written_by_crash_flush():
    candidate = {
        "role": "assistant",
        "content": "candidate already shown to the user",
        "_verification_candidate": True,
    }
    instance = object.__new__(AIAgent)
    instance._session_db = MagicMock()
    instance._session_db_created = True
    instance.session_id = "verification-crash-window"
    instance._last_flushed_db_idx = 0
    instance._flushed_db_message_ids = set()
    instance._flushed_db_message_session_id = None
    setattr(instance, "_persist_disabled", False)
    setattr(instance, "_persist_user_message_idx", None)
    setattr(instance, "_persist_user_message_override", None)
    setattr(instance, "_persist_user_message_timestamp", None)
    setattr(instance, "_pending_cli_user_message", None)

    assert instance._flush_messages_to_session_db([candidate], []) is True

    written = instance._session_db.append_messages_batch.call_args.kwargs["messages"]
    assert len(written) == 1
    assert written[0]["role"] == "assistant"
    assert written[0]["content"] == "candidate already shown to the user"


def test_verify_on_stop_preserves_composed_report_at_budget_limit(agent, monkeypatch):
    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return _response()

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value="verify it"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    _assert_pending_response_survives(agent, result)
    # The assistant response persists (it is real, unflagged content).
    assert not result["messages"][1].get("_verification_stop_synthetic")


def test_pre_verify_preserves_composed_report_at_budget_limit(agent, monkeypatch):
    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return _response()

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: name == "pre_verify"),
        patch(
            "hermes_cli.plugins.get_pre_verify_continue_message",
            return_value="run project tests",
        ),
        patch("agent.verify_hooks.max_verify_nudges", return_value=2),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    _assert_pending_response_survives(agent, result)
    # The assistant response persists (it is real, unflagged content).
    assert not result["messages"][1].get("_pre_verify_synthetic")


def test_intermediate_ack_uses_summary_instead_of_premature_text(agent, monkeypatch):
    agent.valid_tool_names = ["web_search"]
    agent._intent_ack_continuation = True
    agent._looks_like_codex_intermediate_ack = MagicMock(return_value=True)
    agent._interruptible_api_call = lambda _kwargs: _response("I'll inspect the files now")
    agent._handle_max_iterations = MagicMock(return_value="verified summary.")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("inspect /tmp/project")

    assert result["final_response"] == "verified summary."
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    agent._handle_max_iterations.assert_called_once()


def test_later_verified_response_supersedes_pending_report(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter([_response("premature report"), _response("verified final report")])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", None],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"] == "verified final report"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    agent._handle_max_iterations.assert_not_called()


def test_replacement_candidate_resets_preview_state(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter([_response("candidate one"), _response("candidate two")])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._interim_content_was_streamed = lambda text: text == "candidate one"
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", "verify it again"],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"] == "candidate two"
    assert result["response_previewed"] is False


def test_composed_candidate_is_not_marked_previewed_when_only_parts_streamed(
    agent, monkeypatch
):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    receipt = "Fresh verification from this turn passes."
    answers = iter([_response("substantive answer"), _response(receipt)])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._interim_content_was_streamed = lambda _text: True
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", "verify it again"],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"] == (
        "substantive answer\n\n## Verification\n\n" + receipt
    )
    assert result["response_previewed"] is False


def test_candidate_collapse_appends_after_later_tool_protocol_rows():
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "candidate",
            "_verification_candidate": True,
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
    ]
    fake_agent = SimpleNamespace(_db_flush_scan_prefix=[])

    _collapse_verification_candidates(messages, "canonical answer", fake_agent)

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert messages[1]["tool_calls"]
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "canonical answer"
    assert set(messages[-1]) == {"role", "content", "timestamp"}
    assert fake_agent._db_flush_scan_prefix is None


def test_candidate_collapse_ignores_stale_candidates_from_prior_turns():
    messages = [
        {"role": "user", "content": "old task"},
        {
            "role": "assistant",
            "content": "crash-durable old answer",
            "_verification_candidate": True,
        },
        {"role": "user", "content": "new task"},
        {"role": "assistant", "content": "new answer"},
    ]
    original = [dict(message) for message in messages]
    fake_agent = SimpleNamespace(_db_flush_scan_prefix=[])

    collapsed = _collapse_verification_candidates(
        messages,
        "new answer",
        fake_agent,
    )

    assert collapsed is False
    assert messages == original
    assert fake_agent._db_flush_scan_prefix == []


def test_receipt_only_verification_response_keeps_pending_answer(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter([
        _response("Implemented the requested change."),
        _response("Fresh verification from this turn passes:\n\n`pnpm run lint`"),
    ])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", None],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"] == (
        "Implemented the requested change.\n\n"
        "## Verification\n\n"
        "Fresh verification from this turn passes:\n\n`pnpm run lint`"
    )
    assert result["completed"] is True
    agent._handle_max_iterations.assert_not_called()


def test_repeated_verification_blockers_preserve_and_persist_substantive_answer(
    agent, monkeypatch, tmp_path
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=agent.session_id, source="cli")
    agent._session_db = db
    agent.max_iterations = 3
    agent.iteration_budget.max_total = 3
    blocker = "I cannot provide fresh verification evidence for that edit."
    answers = iter(
        [
            _response("The code edit is complete."),
            _response(blocker),
            _response(blocker),
        ]
    )
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", "verify it again", None],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    expected = (
        "The code edit is complete.\n\n"
        "## Verification\n\n"
        "I cannot provide fresh verification evidence for that edit."
    )
    assert result["final_response"] == expected
    assert result["response_transformed"] is True
    assert result["pre_transform_response"] == blocker
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
    ]
    assert result["messages"][-1]["content"] == expected
    assert not any(
        message.get("_verification_candidate")
        for message in result["messages"]
        if isinstance(message, dict)
    )
    persisted = db.get_messages(agent.session_id)
    assert [message["role"] for message in persisted] == ["user", "assistant"], persisted
    assert persisted[-1]["content"] == expected
    db.close()


def test_verification_composition_persists_transformed_canonical_response(
    agent, monkeypatch, tmp_path
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "transformed-state.db")
    db.create_session(session_id=agent.session_id, source="cli")
    agent._session_db = db
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter(
        [
            _response("The code edit is complete."),
            _response("Fresh verification passes."),
        ]
    )
    agent._interruptible_api_call = lambda _kwargs: next(answers)

    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    def transform(text, **_kwargs):
        return f"{text}\n\n[guarded]", True

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", None],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch("hermes_cli.lifecycle.transform_llm_output", side_effect=transform),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"].endswith("[guarded]")
    assert result["messages"][-1]["content"] == result["final_response"]
    persisted_messages = db.get_messages(agent.session_id)
    assert [message["role"] for message in persisted_messages] == [
        "user",
        "assistant",
    ]
    assert persisted_messages[-1]["content"] == result["final_response"]
    db.close()


def test_multiple_verification_retries_publish_each_candidate_once(agent, monkeypatch):
    """Multiple verification retries should publish each candidate once, in order."""
    agent.max_iterations = 3
    agent.iteration_budget.max_total = 3
    answers = iter([
        _response("candidate one"),
        _response("candidate two"),
        _response("candidate three"),
    ])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    # Three nudges, then None (so the third candidate is the final response).
    nudge_side_effects = ["verify it", "verify it", None]

    emitted = []
    agent.interim_assistant_callback = lambda text, **kw: emitted.append(text)

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=nudge_side_effects,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    # Each candidate was emitted as an interim message, in order.
    assert emitted == ["candidate one", "candidate two"]
    # The final response is the last candidate.
    assert result["final_response"] == "candidate three"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    agent._handle_max_iterations.assert_not_called()




def test_verify_on_stop_emits_interim_response_to_ui(agent, monkeypatch):
    """The verify-on-stop path must emit the full response to the UI callback.

    With no streaming set up in this test, _interim_content_was_streamed
    returns False, so already_streamed is False — the callback reports
    content the UI has not seen yet.
    """
    agent._interruptible_api_call = lambda _kwargs: _response("composed report")
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    callback_calls = []

    def capture_callback(text, *, already_streamed=None):
        callback_calls.append({"text": text, "already_streamed": already_streamed})

    agent.interim_assistant_callback = capture_callback

    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value="verify it"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    # The callback was called with the full response text and already_streamed=False
    assert len(callback_calls) == 1
    assert callback_calls[0]["text"] == "composed report"
    assert callback_calls[0]["already_streamed"] is False

    # The candidate persists as the final response.
    assert result["final_response"] == "composed report"


def test_streamed_interim_then_different_summary_not_marked_previewed(agent, monkeypatch):
    """Ordinary interim narration followed by a different non-streamed summary.

    The model streams "I'll inspect the files now" as an intermediate ack.
    _emit_interim_assistant_message is called for this ordinary narration,
    which must NOT set _response_was_previewed. Then _handle_max_iterations
    produces a different summary through the non-streaming Chat Completions
    path. The final result must NOT be marked as previewed — the interim was
    unrelated mid-turn commentary, not the final response — so the CLI renders
    the summary instead of suppressing it. (#65919 review: response-loss blocker)
    """
    agent.valid_tool_names = ["web_search"]
    agent._intent_ack_continuation = True
    agent._looks_like_codex_intermediate_ack = MagicMock(return_value=True)
    agent._interruptible_api_call = lambda _kwargs: _response("I'll inspect the files now")
    agent._handle_max_iterations = MagicMock(return_value="Here is the summary of what I found.")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    emitted = []
    agent.interim_assistant_callback = lambda text, **kw: emitted.append(text)

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("inspect /tmp/project")

    # The final response is the different summary from _handle_max_iterations.
    assert result["final_response"] == "Here is the summary of what I found."
    # CRITICAL: response_previewed must be False — the interim narration was
    # NOT the final response, so the CLI must render the summary.
    assert result["response_previewed"] is False


