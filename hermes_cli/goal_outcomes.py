"""Pre-delivery goal outcomes shared by CLI, gateway, and Desktop/TUI.

Lifecycle decisions are latched in the turn envelope; post-delivery hooks consume
that decision rather than charging/judging the same turn again.
"""
from __future__ import annotations


def bounded_outcome_call(call, timeout):
    """One output-only call with a wall deadline, including route retries/queueing.

    The worker returns data only: after timeout it cannot mutate goal/history or
    deliver anything. Preserve profile/credential ContextVars on the worker.
    """
    import contextvars
    import queue
    import threading
    import time
    from agent.auxiliary_client import aux_stream_deadline
    output = queue.Queue(maxsize=1)
    deadline = time.monotonic() + timeout
    def run():
        try:
            with aux_stream_deadline(deadline):
                output.put((True, call()))
        except Exception as exc:
            output.put((False, exc))
    context = contextvars.copy_context()
    threading.Thread(target=lambda: context.run(run), daemon=True).start()
    try:
        ok, value = output.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("goal outcome deadline exceeded") from None
    if not ok:
        raise value
    return value


def automatic_goal_notices_enabled() -> bool:
    try:
        from hermes_cli.config import load_config
        return bool(((load_config() or {}).get("goals") or {}).get("auto_notices", True))
    except Exception:
        return True


def prepare_goal_turn(manager, agent, result, *, is_current=lambda: True):
    if not isinstance(result, dict) or "_goal_decision" in result:
        return
    from hermes_cli.goals import collect_tool_evidence, count_active_delegations, gather_background_processes, get_goal_control_revision
    result["_goal_decision"] = {}  # even intentional skips must not be rejudged
    def current():
        return is_current() and not getattr(agent, "_interrupt_requested", False)
    if (not current() or result.get("interrupted") or result.get("compression_deferred")
            or result.get("compression_exhausted") or manager is None or not manager.is_active()):
        return
    revision = get_goal_control_revision(manager.session_id)
    text = result.get("final_response") or ""
    evidence = collect_tool_evidence(result)
    exit_reason = str(result.get("turn_exit_reason") or "")
    if exit_reason.startswith("interrupted") or exit_reason in {"review_dispatched", "interpreter_shutdown"}:
        return
    failed = bool(result.get("failed") or result.get("error") or exit_reason in {
        "budget_exhausted", "max_iterations", "iteration_budget_exhausted", "error", "exception",
        "hidden_reasoning_incomplete", "empty_response_exhausted"}
        or exit_reason.startswith("max_iterations_reached("))
    if failed:
        from agent.redact import redact_sensitive_text
        detail = str(result.get("error") or result.get("failure_reason") or "").strip()
        reason = ": ".join(part for part in (exit_reason, detail) if part) or "agent turn failed"
        reason = redact_sensitive_text(reason, force=True)[:1600]
        decision = manager.unexpected_stop(reason)
    elif text.strip():
        decision = manager.evaluate_after_turn(text, user_initiated=True,
            background_processes=gather_background_processes(owner_task_id=manager.session_id),
            active_delegations=count_active_delegations(manager.session_id), tool_evidence=evidence)
    else:
        return  # emptiness alone does not prove failure
    if not current() or get_goal_control_revision(manager.session_id) != revision:
        return
    # Freeze lifecycle BEFORE the output-only classifier, then recheck authority.
    prepared = manager.prepare_goal_outcome(decision, text, tool_evidence=evidence)
    if not current() or get_goal_control_revision(manager.session_id) != revision:
        return
    decision["_goal_authority"] = {
        "session_id": manager.session_id,
        "revision": revision,
        "updated_at": manager.state.updated_at if manager.state else None,
    }
    result["_goal_decision"] = decision
    if prepared != text or decision.get("stop_explanation"):
        from agent.turn_finalizer import synchronize_terminal_response
        synchronize_terminal_response(agent, result, prepared)
    if decision.get("stop_explanation"):
        result["_goal_outcome_prepared"] = True


def prepared_continuation_is_current(decision):
    """A prepared continuation is authority only while its persisted goal is unchanged."""
    from hermes_cli.goals import GoalManager, get_goal_control_revision
    authority = decision.get("_goal_authority") or {}
    session_id = authority.get("session_id")
    if not session_id:
        return False
    manager = GoalManager(session_id)
    return bool(
        manager.is_active() and manager.state
        and not manager.state.user_stopped
        and manager.state.updated_at == authority.get("updated_at")
        and get_goal_control_revision(session_id) == authority.get("revision")
    )


def consume_goal_decision(result, *, allow_continuation=True):
    """None means legacy/unprepared; empty dict means handled, including replay."""
    if not isinstance(result, dict) or "_goal_decision" not in result:
        return None
    if result.get("_goal_decision_consumed"):
        return {}
    result["_goal_decision_consumed"] = True
    decision = result["_goal_decision"]
    if decision.get("should_continue") and (
        not allow_continuation or result.get("interrupted") or result.get("failed")
        or result.get("error") or not prepared_continuation_is_current(decision)
    ):
        return {}
    return decision
