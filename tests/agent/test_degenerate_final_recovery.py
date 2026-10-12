"""Degenerate-final recovery (#103483).

Provider-side collapse: a turn executes its tool work correctly, then ends on ``finish_reason=stop``
with an ENTIRE visible answer that is a fragment — a stray wrong-script word (``пар``), a token
starting mid-punctuation (``?warming up``). Before the fix the loop accepted the fragment as the
answer, the turn reported ``completed``, and an unattended job silently abandoned the task.

The guard rides the existing ack-continuation path: same scope knob (``agent.intent_ack_continuation``,
default ``auto`` = Responses transports), same bounded per-turn counter, same durable interim +
nudge rows. Shape alone cannot prove a collapse, so the predicate is narrow and the nudge asks for
the same answer again when it WAS complete — a false positive costs one call, never the answer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import looks_like_degenerate_final
from agent.conversation_loop import _DEGENERATE_FINAL_NUDGE


GATEWAY_SILENCE_REPROMPT = (
    "[System: The user typed this message and expects a visible reply. Silence markers are only "
    "for gateway-generated turns. Reply now with a short status of what you did or started, "
    "without a silence marker.]"
)


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_dropped_tool_call_recovery)."""
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        # Explicit opt-in: the loop tests exercise the mechanism, not the transport-scoped default.
        agent._intent_ack_continuation = True
        return agent


def _run(agent, stages):
    agent.client.chat.completions.create.side_effect = stages
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("model_tools.handle_function_call", return_value="ok"),
    ):
        return agent.run_conversation("build the workbook")


def _tool_round(call_id):
    from tests.agent.test_run_agent import _mock_response, _mock_tool_call
    return _mock_response(
        content="", finish_reason="tool_calls",
        tool_calls=[_mock_tool_call(name="web_search", arguments="{}", call_id=call_id)],
    )


def _final(text):
    from tests.agent.test_run_agent import _mock_response
    return _mock_response(content=text, finish_reason="stop")


def _user_rows_sent(agent, call_index):
    kwargs = agent.client.chat.completions.create.call_args_list[call_index].kwargs
    return [m["content"] for m in kwargs["messages"] if m.get("role") == "user"]


class TestGatewaySilenceRecovery:
    @pytest.mark.parametrize("marker", ["NO_REPLY", "[SILENT]"])
    def test_user_typed_gateway_silence_marker_reprompts_once(self, loop_agent, marker):
        loop_agent._gateway_session_key = "agent:main:telegram:dm:test"
        result = _run_gateway(loop_agent, [_final(marker), _final("Started the requested work.")])

        assert result["final_response"] == "Started the requested work."
        assert loop_agent.client.chat.completions.create.call_count == 2
        assert _user_rows_sent(loop_agent, 1)[-1] == GATEWAY_SILENCE_REPROMPT
        contents = [m.get("content") for m in result["messages"]]
        assert marker not in contents
        assert GATEWAY_SILENCE_REPROMPT not in contents

    def test_machinery_gateway_silence_does_not_reprompt(self, loop_agent):
        loop_agent._gateway_session_key = "agent:main:telegram:dm:test"
        result = _run_gateway(
            loop_agent, [_final("NO_REPLY")], persist_user_display_kind="internal_notification",
        )

        assert result["final_response"] == "NO_REPLY"
        assert loop_agent.client.chat.completions.create.call_count == 1

    def test_unaddressed_gateway_silence_does_not_reprompt(self, loop_agent):
        loop_agent._gateway_session_key = "agent:main:telegram:dm:test"
        result = _run_gateway(
            loop_agent, [_final("[SILENT]")], persist_user_display_metadata={"reply_expected": False},
        )

        assert result["final_response"] == "[SILENT]"
        assert loop_agent.client.chat.completions.create.call_count == 1

    def test_user_typed_gateway_silence_retry_is_bounded(self, loop_agent):
        loop_agent._gateway_session_key = "agent:main:telegram:dm:test"
        result = _run_gateway(loop_agent, [_final("NO_REPLY"), _final("[SILENT]")])

        assert result["final_response"] == "[SILENT]"
        assert loop_agent.client.chat.completions.create.call_count == 2

    @pytest.mark.parametrize("interrupted", [False, True], ids=["completed", "interrupted"])
    def test_scaffolding_removed_after_tool_continuation_and_next_turn_reprompts(self, loop_agent, interrupted):
        """The re-prompt may continue through a tool call; its marker and nudge must not survive
        mid-history, or the next human turn inherits a spent retry from stale scaffolding."""
        loop_agent._gateway_session_key = "agent:main:telegram:dm:test"
        loop_agent.valid_tool_names = {"web_search"}

        stages = iter([_final("NO_REPLY"), _tool_round("call_1")])

        def model_call(*_args, **_kwargs):
            stage = next(stages, None)
            if stage is not None:
                return stage
            assert loop_agent._session_messages[-1]["role"] == "tool"
            if interrupted:
                loop_agent.interrupt()
            return _final("Workbook built: 3 sheets.")

        first = _run_gateway(loop_agent, model_call)

        if interrupted:
            assert first["interrupted"] is True
        else:
            assert first["final_response"] == "Workbook built: 3 sheets."
        assert loop_agent.client.chat.completions.create.call_count == 3
        history = first["messages"]
        assert not any(m.get("_gateway_silence_reprompt") for m in history)
        assert [m.get("content") for m in history].count(GATEWAY_SILENCE_REPROMPT) == 0
        assert "NO_REPLY" not in [m.get("content") for m in history]
        assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
        assert history[2]["tool_call_id"] == "call_1"
        assert history[2]["content"] == "ok"
        assert not any(m.get("_gateway_silence_reprompt") for m in loop_agent._session_messages)

        loop_agent.client.chat.completions.create.reset_mock()
        second = _run_gateway(
            loop_agent, [_final("NO_REPLY"), _final("Second status.")], conversation_history=history,
        )

        assert second["final_response"] == "Second status."
        assert loop_agent.client.chat.completions.create.call_count == 2
        assert _user_rows_sent(loop_agent, 1)[-1] == GATEWAY_SILENCE_REPROMPT

    def test_stop_gate_continuation_does_not_reset_reprompt_budget(self, loop_agent, monkeypatch):
        """A verification stop gate continues the same turn; the turn still gets one re-prompt."""
        loop_agent._gateway_session_key = "agent:main:telegram:dm:test"
        monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
        nudges = iter(["verify it"])
        with patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=lambda **_kwargs: next(nudges, None),
        ):
            result = _run_gateway(
                loop_agent,
                [_final("NO_REPLY"), _final("Answer one."), _final("NO_REPLY"), _final("Answer two.")],
            )

        assert loop_agent.client.chat.completions.create.call_count == 3
        assert [rows[-1] for rows in map(lambda i: _user_rows_sent(loop_agent, i), range(3))].count(
            GATEWAY_SILENCE_REPROMPT
        ) == 1
        # The second bare marker ends the turn; the gateway replaces it with its fallback.
        assert result["final_response"] == "NO_REPLY"
        assert not any(m.get("_gateway_silence_reprompt") for m in result["messages"])

        import asyncio
        from types import SimpleNamespace
        from gateway.config import GatewayConfig, Platform
        from gateway.run import GatewayRunner
        from gateway.run_turn import _unexpected_silence_reply
        from gateway.session import SessionSource

        runner = GatewayRunner(GatewayConfig())
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="123")
        response, intentional, _ = asyncio.run(runner._hmwa_shape_agent_response(
            result, source, history=[], session_entry=SimpleNamespace(session_id=result.get("session_id")),
            session_key=None, _quick_key=loop_agent._gateway_session_key, run_generation=1,
            _run_start_session_id=result.get("session_id"), _platform_name="telegram", _msg_start_time=0.0,
        ))
        assert response == _unexpected_silence_reply()
        assert intentional is False


def _run_gateway(
    agent, stages, *, persist_user_display_kind=None, persist_user_display_metadata=None,
    conversation_history=None,
):
    agent.client.chat.completions.create.side_effect = stages
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("model_tools.handle_function_call", return_value="ok"),
    ):
        return agent.run_conversation(
            "build the workbook",
            conversation_history=conversation_history,
            persist_user_display_kind=persist_user_display_kind,
            persist_user_display_metadata=persist_user_display_metadata or {},
        )


class TestFragmentAfterToolWork:
    def test_fragment_after_tool_work_reprompts_instead_of_exiting(self, loop_agent):
        result = _run(loop_agent, [_tool_round("call_1"), _final("пар"), _final("Workbook built: 3 sheets.")])

        assert result["final_response"] == "Workbook built: 3 sheets."
        assert loop_agent.client.chat.completions.create.call_count == 3
        # The re-prompt carried the degenerate-final nudge, not the generic ack nudge.
        assert _user_rows_sent(loop_agent, 2)[-1] == _DEGENERATE_FINAL_NUDGE
        # The fragment stays in the transcript as an interim row, the nudge right after it.
        contents = [m.get("content") for m in result["messages"]]
        assert contents.index("пар") + 1 == contents.index(_DEGENERATE_FINAL_NUDGE)

    def test_persistent_collapse_is_bounded_and_keeps_the_fragment(self, loop_agent):
        """One re-prompt per collapse: the nudge row closes the tool-work window, so a second
        fragment is the answer — today's behaviour, never a loop."""
        result = _run(loop_agent, [_tool_round("call_1"), _final("пар"), _final("?warming up")])

        assert loop_agent.client.chat.completions.create.call_count == 3
        assert result["final_response"] == "?warming up"

    def test_fragment_without_tool_work_is_a_plain_answer(self, loop_agent):
        """A chat-only turn that answers tersely is not a collapse; nothing is re-prompted."""
        result = _run(loop_agent, [_final("пар")])

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result["final_response"] == "пар"


class TestLooksLikeDegenerateFinal:
    EN = "Build the workbook and report back."

    @pytest.mark.parametrize("text", ["пар", "?warming up", ",and", "ошибка", "!warming"])
    def test_fragments_in_an_english_conversation(self, text):
        assert looks_like_degenerate_final(text, user_message=self.EN)

    @pytest.mark.parametrize("text", [
        # Terse legitimate answers that reviewers showed a shape-only guard re-prompting (#111472).
        "42", "SQLite", "report.csv", "3.14159", "€12.50", "你好。", "Done.", "True", "a51143fbbe",
        "2026-09-19", "/tmp/out.log", "$5", "#123", "-1", "(a)", ".env", "~/x", "+1", "✅",
        ":8080", "::1", ":)", ";;", ":=", "}", "N/A", "**Done**", "`ok`", "{}", "null",
        "Reading is a skill", "The answer is 43.", "x" * 25, "",
    ])
    def test_terse_answers_are_not_fragments(self, text):
        assert not looks_like_degenerate_final(text, user_message=self.EN)

    @pytest.mark.parametrize("prompt, text", [
        ("把工作簿建好然后告诉我", "是"), ("把工作簿建好然后告诉我", "已完成"),
        ("Собери таблицу и отчитайся", "Готово"), ("Собери таблицу и отчитайся", "да"),
        ("ワークブックを作って報告して", "はい"), ("أنشئ المصنف ثم أخبرني", "تم"),
        ([{"type": "text", "text": "Собери таблицу"}], "нет"),  # multi-part user content
    ])
    def test_terse_answers_in_the_users_own_script_are_not_fragments(self, prompt, text):
        assert not looks_like_degenerate_final(text, user_message=prompt)
