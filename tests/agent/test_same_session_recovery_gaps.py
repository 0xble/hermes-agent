"""Gap coverage for same-session hard-window recovery (pre-send fit, trigger dedupe,
stop fence, iteration budget, intact pause, late summarizer vs committed recovery)."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from tests.agent.test_same_session_recovery import _agent, _seed_tool

_REFRESH = "agent.conversation_compression._refresh_agent_tool_definitions"


def _response(text):
    msg = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None, reasoning=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model")
    resp.usage = None
    return resp


def _chat_agent(db):
    agent = _agent(db)
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _response("done")
    agent.save_trajectories = False
    agent._use_prompt_caching = False
    return agent


def _gate(agent, api_messages, *, messages, api_call_count=1, request_pressure_tokens=1000):
    from agent.turn_preflight_gate import run_preflight_gate
    return run_preflight_gate(
        agent, request_pressure_tokens=request_pressure_tokens, _moa_prepared_request=None,
        pending_moa_prepared_request=None, messages=messages, system_message="", user_message="inspect",
        active_system_prompt=agent._cached_system_prompt, conversation_history=list(messages),
        api_call_count=api_call_count, compression_attempts=0, max_compression_attempts=3,
        effective_task_id="t", final_response=None, failed=False, _turn_exit_reason=None,
        _compression_timeout_exhausted=False, _preflight_compression_blocked=False,
        _provider_overflow_recovery_pending=False, _last_preflight_pressure=None,
        api_messages=api_messages, tools_for_api=[],
    )


def _consume(agent, n):
    from agent.iteration_budget import IterationBudget
    agent.iteration_budget = IterationBudget(10)
    for _ in range(n):
        assert agent.iteration_budget.consume()
    agent._api_call_count = n


# ── Gap 1: pre-send check — the final request must fit before the provider call ──

def test_presend_gate_refuses_unfittable_request_despite_stale_low_pressure(tmp_path):
    """A stale/anchored pressure figure (1k) must not let a hard-window breach reach the provider."""
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    _consume(agent, 2)
    with patch.object(agent, "_compress_context", side_effect=lambda m, *a, **k: (m, "p")) as compress:
        v = _gate(agent, copy.deepcopy(rows), messages=rows, api_call_count=2)
    compress.assert_called_once()
    assert v.action == "break" and v.failed is True
    assert "No provider call was sent" in v.final_response
    assert v._turn_exit_reason == "context_compression_timeout"
    assert v.messages is rows  # retained intact, nothing appended or clipped
    # The provisional call never reached the provider: refunded exactly once.
    assert v.api_call_count == 1 and agent._api_call_count == 1
    assert agent.iteration_budget.used == 1


def test_presend_gate_lets_fitting_request_through_without_recovery(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = [{"role": "user", "content": "inspect only"}]
    with patch.object(agent, "_compress_context") as compress:
        v = _gate(agent, copy.deepcopy(rows), messages=rows)
    compress.assert_not_called()
    assert v.action == "fallthrough" and not v.failed
    assert v.api_call_count == 1


def test_presend_gate_counts_tool_schemas_in_the_final_request(tmp_path):
    """Messages alone fit; the wire request with tool schemas does not → no send."""
    from agent.turn_preflight_gate import run_preflight_gate
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    agent.context_compressor.context_length = 8000
    rows = [{"role": "user", "content": "inspect only"}]
    huge_tools = [{"type": "function", "function": {
        "name": f"t{i}", "description": "d" * 4000, "parameters": {"type": "object", "properties": {}}}}
        for i in range(10)]
    kwargs = dict(
        request_pressure_tokens=10, _moa_prepared_request=None, pending_moa_prepared_request=None,
        messages=rows, system_message="", user_message="inspect", active_system_prompt="",
        conversation_history=list(rows), api_call_count=1, compression_attempts=0, max_compression_attempts=3,
        effective_task_id="t", final_response=None, failed=False, _turn_exit_reason=None,
        _compression_timeout_exhausted=False, _preflight_compression_blocked=False,
        _provider_overflow_recovery_pending=False, _last_preflight_pressure=None,
        api_messages=copy.deepcopy(rows),
    )
    with patch.object(agent, "_compress_context", side_effect=lambda m, *a, **k: (m, "")):
        assert run_preflight_gate(agent, **kwargs, tools_for_api=[]).action == "fallthrough"
        assert run_preflight_gate(agent, **kwargs, tools_for_api=huge_tools).action == "break"


# ── Gap 2: the replayed trigger is not duplicated in transcript or request ──

def test_admitted_trigger_in_history_is_reused_not_reappended(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _chat_agent(db)
    db.append_message(agent.session_id, "user", "earlier ask")
    db.append_message(agent.session_id, "assistant", "earlier answer")
    db.append_message(agent.session_id, "user", "inspect task abc", platform_message_id="telegram:101")
    history = db.get_messages_as_conversation(agent.session_id)
    # In-process admitted shape (agent._session_messages after the turn-start checkpoint).
    history[-1]["platform_message_id"] = history[-1].pop("message_id")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("inspect task abc", conversation_history=history,
                                        persist_user_platform_id="telegram:101")
    assert result["final_response"] == "done"
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == ["earlier ask", "inspect task abc"]
    durable = [r["content"] for r in db.get_messages(agent.session_id, include_inactive=True) if r["role"] == "user"]
    assert durable == ["earlier ask", "inspect task abc"]


def test_retry_after_paused_recovery_keeps_single_trigger(tmp_path):
    """Turn 1 pauses before the provider; the retry of the same platform input sends it once."""
    from agent.turn_context import PreflightCompressionTimedOut
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _chat_agent(db)
    with patch("agent.turn_context_compaction.run_turn_start_compaction",
               side_effect=PreflightCompressionTimedOut("provider call was not sent")):
        first = agent.run_conversation("inspect task abc", persist_user_platform_id="telegram:101")
    assert first["failed"] is True
    agent.client.chat.completions.create.assert_not_called()
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        second = agent.run_conversation("inspect task abc", conversation_history=first["messages"],
                                        persist_user_platform_id="telegram:101")
    assert second["final_response"] == "done"
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == ["inspect task abc"]
    durable = [r["content"] for r in db.get_messages(agent.session_id, include_inactive=True) if r["role"] == "user"]
    assert durable == ["inspect task abc"]


def test_trigger_replayed_from_durable_history_is_not_duplicated(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _chat_agent(db)
    db.append_message(agent.session_id, "user", "earlier ask")
    db.append_message(agent.session_id, "assistant", "earlier answer")
    db.append_message(agent.session_id, "user", "inspect task abc", platform_message_id="telegram:101")
    history = db.get_messages_as_conversation(agent.session_id)
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        agent.run_conversation("inspect task abc", conversation_history=history,
                               persist_user_platform_id="telegram:101")
    durable = [r["content"] for r in db.get_messages(agent.session_id, include_inactive=True) if r["role"] == "user"]
    assert durable == ["earlier ask", "inspect task abc"]


# ── P3: the fail-closed admission is scoped to turns that may compact ──

def test_admission_failure_does_not_fail_a_turn_that_cannot_compact(tmp_path):
    from agent.compression_recovery import admit_before_compaction, turn_may_compact
    from agent.turn_context import PreflightCompressionTimedOut
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    agent.compression_idle_compact_after_seconds = 0
    rows = [{"role": "user", "content": "short ask"}]
    assert not turn_may_compact(agent, rows)
    with patch.object(agent, "_flush_messages_to_session_db", return_value=False) as flush:
        admit_before_compaction(agent, rows, [], rows[0])  # no raise: ordinary turn-start flush owns it
    flush.assert_not_called()
    big = [{"role": "user", "content": "x" * 4_000_000}]
    assert turn_may_compact(agent, big)
    with patch.object(agent, "_flush_messages_to_session_db", return_value=False), pytest.raises(
            PreflightCompressionTimedOut, match="checkpoint"):
        admit_before_compaction(agent, big, [], big[0])


# ── Gap 3: an explicit stop that lands while recovery runs prevents the commit ──

def test_hard_stop_during_recovery_blocks_commit(tmp_path, monkeypatch):
    import agent.compression_recovery as recovery

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    before = db.get_messages(agent.session_id, include_inactive=True)
    real_projection = recovery.recovery_projection
    stopped = []

    def projection_then_stop(a, messages):
        candidate = real_projection(a, messages)
        if recovery.recovery_active(a) and not stopped:
            stopped.append(a.interrupt(hard_cancel=True))  # /stop arrives mid-recovery, pre-commit
        return candidate

    monkeypatch.setattr(recovery, "recovery_projection", projection_then_stop)
    with patch(_REFRESH):
        out, _ = recovery.try_hard_window_recovery(agent, rows, "", agent._cached_system_prompt)
    assert stopped, "stop was never injected inside the recovery attempt"
    assert db.get_messages(agent.session_id, include_inactive=True) == before
    assert not any(r["compacted"] for r in db.get_messages(agent.session_id, include_inactive=True))
    assert [m["content"] for m in out] == [m["content"] for m in rows]
    assert not any("session_search" in (m.get("content") or "") for m in out)
    assert agent._interrupt_requested is True  # the stop itself is not swallowed
    assert agent.session_id == "recovery" and db.get_session_title("recovery") == "Runtime"


# ── Gap 4: recovery does not replenish the iteration budget ──

def test_recovery_commit_does_not_reset_iteration_budget(tmp_path):
    from agent.compression_recovery import try_hard_window_recovery
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    _consume(agent, 4)
    budget = agent.iteration_budget
    with patch(_REFRESH):
        out, _ = try_hard_window_recovery(agent, rows, "", agent._cached_system_prompt)
    assert any("session_search" in (m.get("content") or "") for m in out)  # recovery really committed
    assert agent.iteration_budget is budget
    assert budget.used == 4 and budget.remaining == 6
    assert agent._api_call_count == 4


def test_presend_recovery_refunds_only_the_unsent_call(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    _consume(agent, 4)
    small = [{"role": "user", "content": "inspect only"}]
    with patch.object(agent, "_compress_context", return_value=(small, "p")):
        v = _gate(agent, copy.deepcopy(rows), messages=rows, api_call_count=4)
    assert v.action == "continue" and v.messages is small
    assert v.api_call_count == 3 and agent.iteration_budget.used == 3


# ── Gap 5: all summary routes failed + unfittable → intact pause ──

def test_all_routes_failed_and_unfittable_pauses_intact_with_state(tmp_path):
    from hermes_cli.goals import GoalState, _meta_key
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    rows[-1]["content"] = "MANDATORY INSTRUCTION " * 100000
    db.replace_messages(agent.session_id, rows)
    rows = db.get_messages_as_conversation(agent.session_id)
    goal_json = GoalState(goal="finish task abc", turns_used=3, max_turns=7).to_json()
    db.set_meta(_meta_key("recovery"), goal_json)
    db.set_session_yolo("recovery", True)
    model_config = db.get_session("recovery")["model_config"]
    before = db.get_messages("recovery", include_inactive=True)

    def all_routes_failed(messages, **kw):
        agent.context_compressor._last_compress_aborted = True
        agent.context_compressor._last_summary_error = "primary and fallback summary routes failed"
        return messages

    agent.context_compressor.compress = all_routes_failed
    with patch(_REFRESH):
        out, _ = agent._compress_context(rows, "", approx_tokens=600000)
    assert [m["content"] for m in out] == [m["content"] for m in rows]
    assert db.get_messages("recovery", include_inactive=True) == before
    assert not any(r["compacted"] for r in before)
    assert agent.session_id == "recovery"
    assert db.get_session_title("recovery") == "Runtime" and db.get_session_title_source("recovery") == "user"
    assert db.get_meta(_meta_key("recovery")) == goal_json
    assert db.get_session("recovery")["model_config"] == model_config
    assert not agent._interrupt_requested and not agent._hard_interrupt_requested.is_set()


# ── Gap 6: a late summarizer cannot overwrite a committed recovery ──

def test_late_summarizer_after_recovery_commit_is_discarded(tmp_path):
    from agent.compression_recovery import try_hard_window_recovery
    from agent.conversation_compression import CompressionCommitFence, compress_context

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _agent(db)
    rows = _seed_tool(db, agent.session_id)
    originals = {r["id"]: r["content"] for r in db.get_messages(agent.session_id)}
    entered, release = threading.Event(), threading.Event()

    def slow_summary(messages, **kw):
        entered.set()
        assert release.wait(30), "test never released the summarizer"
        return [{"role": "user", "content": "LATE SUMMARY"}, messages[-1]]

    agent.context_compressor.compress = slow_summary
    late_fence = CompressionCommitFence()
    late = {}

    def run_late():
        try:
            late["result"] = compress_context(agent, copy.deepcopy(rows), "", approx_tokens=352895,
                                              commit_fence=late_fence)
        except BaseException as exc:  # surfaced below
            late["error"] = exc

    with patch(_REFRESH):
        worker = threading.Thread(target=run_late, daemon=True)
        worker.start()
        assert entered.wait(30)
        # Host timeout: cancel the pending commit and free the stale holder's lease (native path).
        assert late_fence.cancel_before_commit() is True
        late_fence.release_cancelled_compression_lock()
        assert db.get_compression_lock_holder(agent.session_id) is None
        recovered, _ = try_hard_window_recovery(agent, copy.deepcopy(rows), "", agent._cached_system_prompt)
        committed = db.get_messages(agent.session_id)
        release.set()
        worker.join(30)
    assert not worker.is_alive()
    assert "error" not in late, late.get("error")
    assert any("session_search" in (m.get("content") or "") for m in recovered)
    assert db.get_messages(agent.session_id) == committed  # recovery boundary unchanged
    all_rows = db.get_messages(agent.session_id, include_inactive=True)
    assert not any("LATE SUMMARY" in (r["content"] or "") for r in all_rows)
    retained = {r["id"]: r["content"] for r in all_rows}
    assert all(retained[rid] == content for rid, content in originals.items())
    assert [m["content"] for m in late["result"][0]] == [m["content"] for m in rows]
    assert agent.session_id == "recovery"


# ── P1: a reused platform id with different content is new input, not a retry ──

def test_same_id_different_content_is_new_input_not_replaced_by_history(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _chat_agent(db)
    db.append_message(agent.session_id, "user", "first version", platform_message_id="telegram:101")
    db.append_message(agent.session_id, "assistant", "first answer")
    history = db.get_messages_as_conversation(agent.session_id)
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        agent.run_conversation("different accepted instruction", conversation_history=history,
                               persist_user_platform_id="telegram:101")
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"][-1] == "different accepted instruction"
    durable = [r["content"] for r in db.get_messages(agent.session_id, include_inactive=True) if r["role"] == "user"]
    assert durable == ["first version", "different accepted instruction"]
