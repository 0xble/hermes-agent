"""A bounded internal disposition turn using native request and tool-execution paths.

Unlike a general conversation, this cannot summarize, compact, title, run memory
hooks, recover via another provider, or ask auxiliary models. Every attempted
physical request consumes the two-request allowance, including a failed attempt.
"""
import logging
import time

logger = logging.getLogger(__name__)


def validate_correction_client(agent, client):
    """Only native SDK HTTP transports with retries disabled can prove the bound.

    Deliberately exact types, not duck typing: virtual adapters/subclasses may
    expose the same surface while launching an external agent or fan-out loop.
    Rechecked on the physical request client as factories can replace it.
    """
    from openai import OpenAI
    from anthropic import Anthropic
    native = ((agent.api_mode in {"chat_completions", "codex_responses"} and type(client) is OpenAI)
              or (agent.api_mode == "anthropic_messages" and type(client) is Anthropic))
    if not native or agent.provider == "moa" or client.max_retries != 0:
        raise ValueError("This transport cannot guarantee a bounded disposition correction")


def run_correction(agent, prompt, system_message, history, task_id):
    from agent.conversation_loop import _LoopState, _run_phase
    from agent.turn_context import _bind_turn_identity, _stage_turn_user_message
    from agent.turn_request_assembly import assemble_api_request
    from agent.turn_api_request import build_api_request
    from agent.turn_tool_round import run_tool_round
    from agent.turn_response_intake import normalize_response_for_agent
    from agent.turn_usage import record_response_usage
    from agent.message_metadata import append_message

    # Reject virtual clients BEFORE staging/persisting a repair turn. API mode is
    # merely a message shape: e.g. copilot-acp uses chat_completions for an agent.
    client = getattr(agent, "_anthropic_client", None) if agent.api_mode == "anthropic_messages" else agent.client
    validate_correction_client(agent, client)
    messages = list(history or [])
    if not messages or messages[-1].get("role") != "assistant":
        raise ValueError("Disposition correction requires a completed assistant boundary")
    effective_task_id, turn_id = _bind_turn_identity(agent, task_id, None, None, None, None)
    user_msg, _ = _stage_turn_user_message(
        agent, prompt, None, None, None, "hidden", {"delegation_disposition_correction": True})
    append_message(messages, user_msg)
    agent._persist_user_message_idx = len(messages) - 1
    agent._session_messages = messages
    agent._incremental_persistence_failed = False
    agent._tool_guardrail_halt_decision = None
    if agent._flush_messages_to_session_db(messages, history) is False:
        raise RuntimeError("Could not persist disposition correction boundary")
    s = _LoopState(
        messages=messages, conversation_history=history, user_message=prompt,
        original_user_message=prompt, system_message=system_message,
        active_system_prompt=agent._cached_system_prompt,
        effective_task_id=effective_task_id, turn_id=turn_id,
        current_turn_user_idx=len(messages) - 1, request_logger=logger,
        moa_config=None, _should_review_memory=False, _plugin_user_context="",
        _ext_prefetch_cache="", _preflight_compression_blocked=False, max_compression_attempts=0,
    )
    try:
        for _ in range(min(agent.max_iterations, 2)):
            if agent._interrupt_requested:
                break
            _run_phase(assemble_api_request, agent, s)
            s.api_start_time = time.time()
            s.api_request_id = agent._current_api_request_id = f"{turn_id}:correction:{s.api_call_count}"
            _run_phase(build_api_request, agent, s)
            # Native SDK request clients explicitly use max_retries=0. No outer
            # retry/recovery path here; a failed attempt is counted and ends repair.
            s.api_call_count += 1
            try:
                response = agent._interruptible_api_call(s.api_kwargs)
            except Exception:
                logger.exception("Disposition correction request failed")
                break
            record_response_usage(agent, response, messages=messages,
                                  api_call_count=s.api_call_count,
                                  api_duration=time.time() - s.api_start_time,
                                  compression_attempts=0, max_compression_attempts=0)
            s.assistant_message = normalize_response_for_agent(agent, response)
            s.finish_reason = s.assistant_message.finish_reason
            if not s.assistant_message.tool_calls:
                append_message(messages, {**agent._build_assistant_message(s.assistant_message, s.finish_reason),
                                          "display_kind": "hidden"})
                break
            # Normal validation may auto-repair/print a hallucinated name or
            # force-print malformed argument diagnostics. Neither belongs to a
            # hidden ledger correction; reject the batch without those channels.
            import json
            assistant = s.assistant_message
            try:
                malformed = any(tc.function.name not in agent.valid_tool_names or not isinstance(
                    json.loads(tc.function.arguments or "{}"), dict) for tc in assistant.tool_calls)
            except (TypeError, ValueError):
                malformed = True
            if malformed:
                from agent.tool_dispatch_helpers import make_tool_result_message
                agent._uniquify_tool_call_ids(assistant.tool_calls)
                message = agent._build_assistant_message(assistant, s.finish_reason)
                message["display_kind"] = "hidden"
                append_message(messages, message)
                for tc in assistant.tool_calls:
                    receipt = make_tool_result_message(tc.function.name,
                        "Correction requires a valid disposition handle call; no tool was executed.", tc.id)
                    receipt["display_kind"] = "hidden"
                    append_message(messages, receipt)
                agent._flush_messages_to_session_db(messages, history)
                continue
            verdict = _run_phase(run_tool_round, agent, s)
            if verdict.action in {"break", "return"}:
                break
    except Exception:
        logger.exception("Disposition correction ended without a verified disposition")
    finally:
        # Close the internal turn without a summary request, even on two tool
        # responses. Hidden output never replaces or previews the original answer.
        if messages[-1].get("role") != "assistant":
            append_message(messages, {"role": "assistant", "content": "", "display_kind": "hidden"})
        agent._persist_session(messages, history)
    return {"messages": messages, "api_calls": s.api_call_count, "final_response": ""}
