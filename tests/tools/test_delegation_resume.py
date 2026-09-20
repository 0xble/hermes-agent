"""delegate_task(action='resume') — explicit, one-shot recovery of an interrupted
background delegation (maintenance/delegation-restart.md slice 2, narrow form).

Contract pinned here:

* Eligibility is durable and narrow: only single-task rows whose owner stopped
  without a trustworthy terminal result, with a routable origin and no recorded
  partial child results.
* The claim is durable and ONE-SHOT: a second resume of the same delegation is
  refused even across a fresh process/connection.
* Only the owning conversation can claim; a foreign caller is refused BEFORE the
  claim is spent.
* The returned brief requires state verification and never spawns anything.
* No credentials are read from or written to the ledger.
* Boot auto-trigger queues one parent-facing notice for eligible rows, but
  never claims ``resume_state`` or starts a child itself.
"""

from __future__ import annotations

import json
import time

import pytest

from tools import async_delegation as ad
from tools import delegation_resume as dr
from tools.delegate_tool import _handle_control_action, delegate_task


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    """Point the durable ledger at a throwaway state.db for every test."""
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    yield


class _Parent:
    def __init__(self, session_id="sess-owner"):
        self.session_id = session_id
        self._session_db = None


def _dispatch_row(delegation_id="deleg_resume_1", *, goal="port the widget", context="original ctx",
                  parent_session_id="sess-owner", origin_session_id="sess-owner", is_batch=False,
                  goals=None):
    record = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": None,
        "role": "leaf",
        "model": "m",
        "session_key": "telegram:dm:1",
        "origin_ui_session_id": "",
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        "dispatched_at": time.time() - 60.0,
    }
    if is_batch:
        record["is_batch"] = True
        record["goals"] = goals or ["a", "b"]
    ad._persist_dispatch(record)
    return delegation_id


def _mark(delegation_id, state, result=None):
    """Force a terminal ledger state, as the abandon/stall/interrupt paths do."""
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET state=?, completed_at=?, updated_at=?, event_json=?, result_json=? "
            "WHERE delegation_id=?",
            (state, now, now, json.dumps({"status": state}), json.dumps(result or {"status": state}), delegation_id),
        )


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(dr.RESUMABLE_STATES))
def test_interrupted_single_task_is_eligible(state):
    did = _dispatch_row(f"deleg_elig_{state}")
    _mark(did, state)
    record, reason = dr.inspect_resumable(did)
    assert reason is None
    assert record["state"] == state


def test_running_delegation_is_not_resumable():
    did = _dispatch_row("deleg_running")  # still 'running' from dispatch
    _, reason = dr.inspect_resumable(did)
    assert reason == dr.INELIGIBLE_STATE


def test_completed_delegation_is_not_resumable():
    did = _dispatch_row("deleg_done")
    _mark(did, "completed")
    _, reason = dr.inspect_resumable(did)
    assert reason == dr.INELIGIBLE_STATE


def test_batch_delegation_is_refused():
    did = _dispatch_row("deleg_batch", is_batch=True)
    _mark(did, "unknown")
    _, reason = dr.inspect_resumable(did)
    assert reason == dr.INELIGIBLE_BATCH


def test_partial_child_results_are_refused():
    did = _dispatch_row("deleg_partial")
    _mark(did, "unknown", result={"results": [{"task_index": 0, "status": "completed"}]})
    _, reason = dr.inspect_resumable(did)
    assert reason == dr.INELIGIBLE_PARTIAL


def test_stateless_origin_is_refused():
    did = _dispatch_row("deleg_cron", parent_session_id=None, origin_session_id="")
    _mark(did, "unknown")
    _, reason = dr.inspect_resumable(did)
    assert reason == dr.INELIGIBLE_STATELESS


def test_unknown_delegation_id_is_refused():
    _, reason = dr.inspect_resumable("deleg_never_existed")
    assert reason == dr.INELIGIBLE_NO_ROW
    _, reason_empty = dr.inspect_resumable("")
    assert reason_empty == dr.INELIGIBLE_NO_ROW


def test_row_without_goal_is_refused():
    did = _dispatch_row("deleg_nogoal", goal="")
    _mark(did, "unknown")
    _, reason = dr.inspect_resumable(did)
    assert reason == dr.INELIGIBLE_STATE


# ---------------------------------------------------------------------------
# Durable one-shot claim
# ---------------------------------------------------------------------------


def test_claim_is_one_shot_and_durable():
    did = _dispatch_row("deleg_claim_once")
    _mark(did, "unknown")

    record, reason = dr.claim_resume(did)
    assert reason is None
    assert record["resume_attempts"] == 1

    # Second claim in the same process.
    _, reason2 = dr.claim_resume(did)
    assert reason2 == dr.INELIGIBLE_CLAIMED

    # And the refusal is DURABLE: a fresh read of the ledger (new connection)
    # still sees the spent claim, so a restart cannot re-issue recovery.
    record3, reason3 = dr.inspect_resumable(did)
    assert reason3 == dr.INELIGIBLE_CLAIMED
    assert record3["resume_state"] == dr.RESUME_STATE_CLAIMED
    assert record3["resume_attempts"] == 1


def test_claim_persists_no_credentials():
    did = _dispatch_row("deleg_nocreds")
    _mark(did, "unknown")
    dr.claim_resume(did)
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(
            "SELECT task_json, result_json, resume_claim FROM async_delegations WHERE delegation_id=?",
            (did,),
        ).fetchone()
    blob = " ".join(str(c) for c in row)
    for secret_marker in ("api_key", "apiKey", "token", "secret", "password", "base_url"):
        assert secret_marker not in blob
    # The claim token is an opaque consumer:pid:uuid handle, not a credential.
    assert row[2].startswith("delegate_task:")


def test_claim_refuses_ineligible_without_spending_an_attempt():
    did = _dispatch_row("deleg_batch_claim", is_batch=True)
    _mark(did, "unknown")
    _, reason = dr.claim_resume(did)
    assert reason == dr.INELIGIBLE_BATCH
    record, _ = dr.inspect_resumable(did)
    assert record["resume_attempts"] == 0
    assert record["resume_state"] == dr.RESUME_STATE_NONE


# ---------------------------------------------------------------------------
# Recovery instruction
# ---------------------------------------------------------------------------


def test_recovery_instruction_requires_state_verification():
    did = _dispatch_row("deleg_brief", goal="publish the release notes", context="repo X")
    _mark(did, "interrupted")
    record, reason = dr.claim_resume(did)
    assert reason is None
    text = dr.build_recovery_instruction(record)
    lowered = text.lower()
    assert "verify" in lowered
    assert "never repeat an external write" in lowered
    assert "do not assume a clean starting point" in lowered
    assert "publish the release notes" in text
    assert "repo X" in text
    assert did in text


# ---------------------------------------------------------------------------
# delegate_task(action='resume') control path
# ---------------------------------------------------------------------------


def test_resume_action_returns_brief_and_spawns_nothing(monkeypatch):
    did = _dispatch_row("deleg_action_ok")
    _mark(did, "unknown")

    import tools.delegate_tool_dispatch as dtd

    def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("action='resume' must not reach spawn machinery")

    monkeypatch.setattr(dtd, "_run_batch", _explode)

    out = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert out["action"] == "resume"
    assert out["status"] == "recovery_claimed"
    assert out["goal"] == "port the widget"
    assert "verify" in out["recovery_context"].lower()
    assert "one-shot" in out["note"]


def test_resume_action_requires_subagent_id():
    out = _handle_control_action("resume", "  ", None, _Parent())
    assert "requires subagent_id" in out


def test_resume_action_refuses_foreign_conversation_without_spending_claim():
    did = _dispatch_row("deleg_foreign")
    _mark(did, "unknown")

    out = _handle_control_action("resume", did, None, _Parent(session_id="sess-intruder"))
    assert "does not belong to this conversation" in out

    # The owner's one claim is intact.
    record, reason = dr.inspect_resumable(did)
    assert reason is None
    assert record["resume_attempts"] == 0
    owner_out = json.loads(_handle_control_action("resume", did, None, _Parent()))
    assert owner_out["status"] == "recovery_claimed"


def test_resume_action_refuses_parent_without_session_id():
    did = _dispatch_row("deleg_no_parent_sid")
    _mark(did, "unknown")

    class _Anonymous:
        pass

    out = _handle_control_action("resume", did, None, _Anonymous())
    assert "does not belong to this conversation" in out


def test_resume_action_second_call_is_refused():
    did = _dispatch_row("deleg_action_twice")
    _mark(did, "unknown")
    parent = _Parent()
    first = json.loads(_handle_control_action("resume", did, None, parent))
    assert first["status"] == "recovery_claimed"
    second = _handle_control_action("resume", did, None, parent)
    assert "already been claimed" in second


def test_resume_action_batch_refusal_is_truthful():
    did = _dispatch_row("deleg_action_batch", is_batch=True)
    _mark(did, "unknown")
    out = _handle_control_action("resume", did, None, _Parent())
    assert "multi-task batch" in out


def test_resume_routes_through_delegate_task_entrypoint():
    did = _dispatch_row("deleg_entrypoint")
    _mark(did, "stalled")
    out = json.loads(delegate_task(action="resume", subagent_id=did, parent_agent=_Parent()))
    assert out["action"] == "resume"
    assert out["delegation_id"] == did


def test_real_single_task_dispatch_keeps_scalar_recovery_shape(monkeypatch):
    """A production one-goal background dispatch must remain resumable as scalar work."""
    import threading
    from tools import async_delegation as ad

    started = threading.Event()
    release = threading.Event()

    def runner():
        started.set()
        release.wait(2)
        return {"summary": "done", "api_calls": 0, "duration_seconds": 0.1}

    handle = ad.dispatch_async_delegation_batch(
        goals=["port the widget"], context="original ctx", toolsets=None,
        role="leaf", model="m", session_key="telegram:dm:1",
        parent_session_id="sess-owner", origin_session_id="sess-owner",
        delegation_id="deleg_real_single", runner=runner,
        max_async_children=1,
    )
    assert handle["status"] == "dispatched"
    assert started.wait(1)
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(
            "SELECT state, task_json FROM async_delegations WHERE delegation_id=?",
            ("deleg_real_single",),
        ).fetchone()
    task = json.loads(row[1])
    assert row[0] == "running"
    assert task.get("is_batch") is True and task.get("goals") == ["port the widget"]
    release.set()


def test_resume_does_not_consume_the_spawn_cap():
    from agent.tool_guardrails import _subagent_spawn_count

    assert _subagent_spawn_count({"action": "resume", "subagent_id": "deleg_x"}) == 0


def test_boot_auto_trigger_is_enabled_but_parent_only():
    assert dr.AUTO_RESUME_ON_BOOT is True


def test_boot_candidates_keep_explicit_resume_eligible_rows_narrow():
    good = _dispatch_row("deleg_boot_good")
    _mark(good, "unknown")
    batch = _dispatch_row("deleg_boot_batch", is_batch=True)
    _mark(batch, "unknown")
    partial = _dispatch_row("deleg_boot_partial")
    _mark(partial, "unknown", result={"results": [{"task_index": 0, "status": "completed"}]})
    stateless = _dispatch_row("deleg_boot_stateless", parent_session_id=None, origin_session_id="")
    _mark(stateless, "unknown")

    candidates = dr.list_boot_candidates()
    assert [row["delegation_id"] for row in candidates] == [good]


def test_boot_trigger_is_one_shot_and_does_not_spend_resume_claim():
    did = _dispatch_row("deleg_boot_claim")
    _mark(did, "unknown")

    record, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert record is not None
    assert record["auto_resume_state"] == dr.AUTO_RESUME_STATE_CLAIMED
    assert record["resume_state"] == dr.RESUME_STATE_NONE
    assert dr.complete_auto_resume_trigger(did, record["auto_resume_claim"]) is True

    _, second_reason = dr.claim_auto_resume_trigger(did)
    assert second_reason == "already_triggered"
    assert dr.claim_resume(did)[1] is None


def test_boot_notice_is_parent_instruction_without_task_context_or_credentials():
    did = _dispatch_row("deleg_boot_notice", context="secret context must not be copied")
    _mark(did, "unknown")
    record, reason = dr.claim_auto_resume_trigger(did)
    assert reason is None
    assert record is not None
    text = dr.build_auto_resume_notice(record)
    assert did in text
    assert "action='resume'" in text
    assert "secret context" not in text
    assert "do not execute the original task directly" in text.lower()
