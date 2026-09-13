"""Turn-boundary disposition repair; reuses the card ledger and admitted turn.

No mid-loop prompt edits or fabricated user messages. A completed loop may be
followed by ONE typed internal turn, with a two-call bound and original-answer
fallback. No global unresolved-result scan or automatic acknowledgement.
"""
import json
from contextlib import contextmanager
import logging
import uuid

logger = logging.getLogger(__name__)

# Shared by the tool schema, correction prompt and runtime validation error.
DEFER_REASON_GUIDANCE = (
    "defer_reason must be a short state label of 1-2 words (at most 160 characters), "
    "e.g. Under review, Awaiting CI, or Review failed. "
    "Put any longer explanation in the normal response, not the card label."
)


def _query(agent, results=None, *, include_deferred=False):
    callback = getattr(agent, "tool_progress_callback", None)
    if not callable(callback):
        return {"missing": []}
    from tools.async_delegation import current_delegation_owner
    answer = callback("subagent.result_turn", actor_session_id=str(agent.session_id),
                      actor_owner=current_delegation_owner(agent),
                      turn_id=agent._delegation_result_turn, results=results,
                      **({"include_deferred": True} if include_deferred else {}))
    # Display-only callbacks on CLI surfaces have no lifecycle ledger. Gateway
    # lifecycle relays return a mapping; wrappers must preserve that return value.
    return answer if isinstance(answer, dict) else {"missing": []}


def begin_result_turn(agent, metadata=None, *, include_deferred=True):
    agent._delegation_result_turn = uuid.uuid4().hex
    agent._delegation_result_tracking_error = False
    agent._delegation_followthrough_done = False
    results = (metadata or {}).get("delegation_results")
    if results:
        try:
            answer = _query(agent, results, include_deferred=include_deferred)
            if include_deferred:
                agent._delegation_followthrough_done = True
            return _followthrough(agent, answer)
        except Exception:
            agent._delegation_result_tracking_error = True
            logger.exception("Could not persist this turn's delegation result delivery")
    return ""


def _followthrough(agent, answer):
    if answer.get("deferred"):
        from agent.delegation_followthrough import retrieve_deferred_context
        content, presentations = retrieve_deferred_context(agent, answer["deferred"])
        if presentations:
            _query(agent, presentations)
        return content
    return ""


def observe_tool_results(agent, assistant_message, messages):
    if not isinstance(getattr(agent, "_delegation_result_turn", None), str):
        return
    calls = {tc.id for tc in assistant_message.tool_calls if tc.function.name == "delegate_task"}
    followthrough = ""
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") not in calls:
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        # Dispatch handles are not terminal results delivered to the model.
        if not isinstance(payload, dict):
            continue
        entries = payload.get("results", payload.get("inline_results"))
        if not isinstance(entries, list):
            continue
        metadata = payload.get("delegation_metadata") or payload
        refs = metadata.get("thread_refs")
        if not metadata.get("parent_task_id") or not isinstance(refs, list):
            continue
        indices = {e.get("task_index") for e in entries if isinstance(e, dict)}
        threads = metadata.get("threads")
        exact = ([t["thread_ref"] for i, t in enumerate(threads)
                  if isinstance(t, dict) and t.get("task_index", i) in indices]
                 if isinstance(threads, list) else [ref for i, ref in enumerate(refs) if i in indices])
        try:
            include = not getattr(agent, "_delegation_followthrough_done", False) and not getattr(agent, "_review_yield_requested", False)
            answer = _query(agent, [{"parent_task_id": metadata["parent_task_id"], "thread_refs": exact,
                            "attempts": metadata.get("attempts", {})}], include_deferred=include)
            if include:
                agent._delegation_followthrough_done = True
                followthrough = _followthrough(agent, answer)
        except Exception:
            agent._delegation_result_tracking_error = True
            logger.exception("Could not persist exact delegate tool result presentation")
    return followthrough


@contextmanager
def isolated_correction_channels(agent):
    """The correction is ledger work, never another host message (including TTS)."""
    names = ("stream_delta_callback", "_stream_callback", "interim_assistant_callback",
             "reasoning_callback", "thinking_callback", "status_callback", "warning_callback",
             "step_callback", "reaction_callback", "tool_progress_callback",
             "tool_start_callback", "tool_complete_callback", "quiet_mode", "_disable_streaming",
             "suppress_status_output", "_print_fn")
    absent = object()
    saved = {name: getattr(agent, name, absent) for name in names}
    progress = saved["tool_progress_callback"]
    def ledger_only(event, *args, **kwargs):
        if event in {"subagent.handling", "subagent.result_turn"} and callable(progress):
            return progress(event, *args, **kwargs)
    try:
        for name in names:
            setattr(agent, name, None)
        agent.quiet_mode = True
        agent._disable_streaming = True
        agent.suppress_status_output = True
        agent._print_fn = lambda *args, **kwargs: None
        agent.tool_progress_callback = ledger_only
        yield
    finally:
        for name, value in saved.items():
            if value is absent:
                delattr(agent, name)
            else:
                setattr(agent, name, value)


def finish_result_turn(agent, original, run_turn, system_message, task_id):
    """Run only after the original loop returned, before the host seals delivery."""
    if not isinstance(original, dict) or original.get("interrupted") or original.get("failed"):
        return original
    try:
        missing = _query(agent).get("missing", [])
    except Exception:
        missing = []
        agent._delegation_result_tracking_error = True
    if not missing and not agent._delegation_result_tracking_error:
        return original
    if missing and not getattr(agent, "_interrupt_requested", False):
        prompt = (
            "[Internal delegation disposition correction — not a user request]\n"
            "The previous processing turn returned an answer but omitted disposition for the exact results below. "
            "Do not redo their work or start unrelated work. Use delegate_task(action='handle') for each result: "
            "incorporated only if actually used in the answer, blocker_report only if the answer reports its blocker, "
            f"otherwise deferred. {DEFER_REASON_GUIDANCE} Do not auto-accept a result. "
            "A revision requires an explicitly authorized successful same-session continuation, not a handle claim. "
            "The original answer is preserved for delivery; finish with no additional user-facing prose.\n"
            + json.dumps(missing, ensure_ascii=False)
        )
        previous_limit = agent.max_iterations
        try:
            agent.max_iterations = min(previous_limit, 2)
            agent._delegation_disposition_correction = {}
            for item in missing:
                agent._delegation_disposition_correction.setdefault(item["parent_task_id"], []).append(item["thread_ref"])
            with isolated_correction_channels(agent):
                correction = run_turn(agent, prompt, system_message, original.get("messages"), task_id,
                                      persist_user_display_kind="hidden",
                                      persist_user_display_metadata={"delegation_disposition_correction": True})
            # Preserve the real correction transcript/accounting, never its prose in
            # place of the already-composed answer. No recursive final gate.
            if isinstance(correction, dict):
                original = {**original, "messages": correction.get("messages", original.get("messages")),
                            "api_calls": original.get("api_calls", 0) + correction.get("api_calls", 0)}
            missing = _query(agent).get("missing", [])
        except Exception:
            logger.exception("Bounded delegation disposition correction failed")
        finally:
            agent.max_iterations = previous_limit
            agent._delegation_disposition_correction = None
    if missing or agent._delegation_result_tracking_error:
        warning = ("Some delegated results still need an explicit disposition. They remain visible; "
                   "review them and mark incorporated, report a blocker, or defer with a reason.")
        original = {**original, "delegation_disposition_unresolved": True,
                    "final_response": (original.get("final_response") or "") + "\n\n" + warning}
        # Existing host warning channel, with the same honest note in the final
        # payload before streaming seal. No visible internal identifiers.
        try:
            agent._emit_warning(warning)
        except Exception:
            logger.exception("Disposition warning callback failed; final response retains the warning")
    return original
