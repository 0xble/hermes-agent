"""Result payload retention follows card disposition delivery, not notification ack."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from gateway.delegation_cards import DelegationCards
from gateway.config import Platform
from gateway.session import SessionSource
from tools import async_delegation as ad
from tools.delegate_tool import delegate_task


@pytest.mark.asyncio
@pytest.mark.parametrize("pruning", ["age", "capacity"])
@pytest.mark.parametrize("delivery", ["delivered", "dropped", "inline"])
async def test_deferred_result_survives_notification_ack_and_pruning_until_exact_delivery(tmp_path, monkeypatch, pruning, delivery):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "async_delegations.db")
    ad._reset_for_tests()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    runner = SimpleNamespace(_adapter_for_source=lambda _: None)
    cards = DelegationCards(runner, home=tmp_path, interval=0)
    owner = {"profile": "default", "session_id": "s", "session_key": "r", "chat_id": "42",
             "thread_id": "", "topic_id": ""}
    parent_task_id = "a" * 32
    metadata = {
        "parent_task_id": parent_task_id, "owner": owner,
        "owner_json": json.dumps(owner, sort_keys=True, separators=(",", ":")),
        "thread_refs": ["A"], "task_labels": ["Preserve result"], "attempts": {"A": 0},
        "threads": [{"thread_ref": "A", "task_index": 0, "task_label": "Preserve result"}],
    }
    uid = "deleg_retained"
    ad._persist_dispatch({"delegation_id": uid, "session_key": "r", "parent_session_id": "s",
                          "dispatched_at": time.time() - 10, "delegation_metadata": metadata})
    ad._persist_completion({"type": "async_delegation", "delegation_id": uid, "status": "completed",
                            "completed_at": time.time() - 9},
                           {"results": [{"task_index": 0, "status": "completed", "summary": "payload"}]})
    assert ad.claim_completion_delivery(uid, "notification")
    assert ad.complete_completion_delivery(uid, "notification")  # notification admission/ack only
    with ad._transaction() as conn:
        if delivery == "inline":
            conn.execute("UPDATE async_delegations SET event_json=NULL, delivery_state='pending' WHERE delegation_id=?", (uid,))
        elif delivery == "dropped":
            conn.execute("UPDATE async_delegations SET delivery_state='dropped' WHERE delegation_id=?", (uid,))
    # Ack may precede the actual parent turn by arbitrarily long. No presentation
    # callback may be required to preserve an unresolved payload.
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET updated_at=? WHERE delegation_id=?",
                     (0 if pruning == "age" else time.time() - 1, uid))
        for index in range(ad._MAX_RETAINED_COMPLETED + 1):
            conn.execute("""INSERT INTO async_delegations
                (delegation_id, origin_session, state, dispatched_at, completed_at, updated_at,
                 task_json, delivery_state)
                VALUES (?, 'r', 'completed', 0, 0, ?, '{}', 'delivered')""",
                         (f"early-settled-{index}", time.time()))
    ad._prune_durable_records()
    assert ad.get_durable_delegation(uid) is not None

    data = {"parent_task_id": parent_task_id, "thread_ref": "A", "task_label": "Preserve result", "owner": owner}
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    await cards.result_turn(actor_session_id="s", turn_id="arrival", results=[{
        "parent_task_id": parent_task_id, "thread_refs": ["A"], "attempts": {"A": 0},
    }])
    await cards.handling(source, "r", "s", 2, actor_session_id="s", parent_task_id=parent_task_id,
                         refs=["A"], reason="deferred", detail="Need approval", turn_id="arrival")

    # Exercise both age and >50-settled pruning paths without changing delivery state.
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET updated_at=? WHERE delegation_id=?",
                     (0 if pruning == "age" else time.time() - 1, uid))
        for index in range(ad._MAX_RETAINED_COMPLETED + 1):
            conn.execute("""INSERT INTO async_delegations
                (delegation_id, origin_session, state, dispatched_at, completed_at, updated_at,
                 task_json, delivery_state)
                VALUES (?, 'r', 'completed', 0, 0, ?, '{}', 'delivered')""",
                         (f"settled-{index}", time.time()))
    ad._prune_durable_records()
    retained = ad.get_durable_delegation(uid)
    assert retained is not None
    assert retained["result"]["results"][0]["summary"] == "payload"

    # A restored card and the owner-scoped handle can reopen the deferred payload.
    restored = DelegationCards(runner, home=tmp_path, interval=0)
    tokens = None
    from gateway.session_context import clear_session_vars, set_session_vars
    tokens = set_session_vars(platform="telegram", profile="default", chat_id="42", session_key="r", session_id="s")
    try:
        parent = SimpleNamespace(session_id="s")
        payload = json.loads(await asyncio.to_thread(delegate_task, action="result", delegation_id=uid, parent_agent=parent))
        assert payload["results"][0]["summary"] == "payload"
        from agent.delegation_followthrough import retrieve_deferred_context
        wanted = [{"parent_task_id": parent_task_id, "thread_ref": "A", "attempt": 0}]
        context, presentations = await asyncio.to_thread(retrieve_deferred_context, parent, wanted)
        assert "payload" in context and uid in context
        assert presentations == [{"parent_task_id": parent_task_id, "thread_refs": ["A"], "attempts": {"A": 0}}]
        assert await asyncio.to_thread(retrieve_deferred_context, parent,
            [{**wanted[0], "attempt": 1}]) == ("", [])
        assert await asyncio.to_thread(retrieve_deferred_context,
            SimpleNamespace(session_id="foreign"), wanted) == ("", [])
        assert not restored.cards[parent_task_id].get("handled")
        await restored.handling(source, "r", "s", 3, actor_session_id="s", parent_task_id=parent_task_id,
                               refs=["A"], reason="incorporated", turn_id="arrival")
        await restored.delivered({parent_task_id: restored._proof(restored.cards[parent_task_id], ["A"])})
    finally:
        clear_session_vars(tokens)

    # Exact parent-final delivery releases retention, so settled payloads prune normally.
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET updated_at=0 WHERE delegation_id=?", (uid,))
        for index in range(ad._MAX_RETAINED_COMPLETED + 1):
            conn.execute("""INSERT INTO async_delegations
                (delegation_id, origin_session, state, dispatched_at, updated_at, delivery_state)
                VALUES (?, 'r', 'completed', 0, ?, 'dropped')""", (f"late-settled-{index}", time.time()))
    ad._prune_durable_records()
    assert ad.get_durable_delegation(uid) is None
    ad._reset_for_tests()


def test_single_result_retention_and_exact_owner_attempt_release(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "async_delegations.db")
    owner = {"session_id": "parent", "profile": "default"}
    metadata = {"parent_task_id": "task", "owner": owner,
                "owner_json": json.dumps(owner, sort_keys=True, separators=(",", ":")),
                "thread_refs": ["A"], "attempts": {"A": 2},
                "threads": [{"thread_ref": "A", "task_index": 0}]}
    ad._persist_dispatch({"delegation_id": "single", "session_key": "r", "dispatched_at": 0,
                          "delegation_metadata": metadata})
    ad._persist_completion({"delegation_id": "single", "status": "completed"},
                           {"summary": "single payload", "status": "completed"})
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET delivery_state='delivered', updated_at=0")
    ad._prune_durable_records()
    assert ad.get_durable_delegation("single")["result"]["summary"] == "single payload"
    assert ad.release_result_retention(owner={**owner, "session_id": "foreign"},
        parent_task_id="task", attempts={"A": 2}) == 0
    assert ad.release_result_retention(owner=owner, parent_task_id="task", attempts={"A": 1}) == 0
    assert ad.release_result_retention(owner=owner, parent_task_id="task", attempts={"B": 2}) == 0
    assert ad.release_result_retention(owner=owner, parent_task_id="task", attempts={"A": 2}) == 1
    assert ad.release_result_retention(owner=owner, parent_task_id="task", attempts={"A": 2}) == 0
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET updated_at=0")
    ad._prune_durable_records()
    assert ad.get_durable_delegation("single") is None


@pytest.mark.parametrize("pruning", ["age", "capacity"])
def test_inline_archive_release_bounds_history_without_async_dispatch(tmp_path, monkeypatch, pruning):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "async_delegations.db")
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 2)
    clock = [time.time()]
    monkeypatch.setattr(ad.time, "time", lambda: clock[0])
    owner = {"session_id": "parent", "profile": "default"}
    metadata = {"parent_task_id": "task", "owner": owner,
        "owner_json": json.dumps(owner, sort_keys=True, separators=(",", ":")),
        "threads": [{"thread_ref": "A", "task_index": 0}], "attempts": {"A": 0}}
    result = {"results": [{"task_index": 0, "status": "completed", "summary": "payload"}]}
    unresolved = ad.persist_inline_result(result, metadata)
    settled = []
    for index in range(5):
        if pruning == "age":
            clock[0] += ad._DURABLE_RETENTION_SECONDS + 1
        else:
            clock[0] += 1
        current = {**metadata, "parent_task_id": f"settled-{index}"}
        uid = ad.persist_inline_result(result, current)
        settled.append(uid)
        assert ad.release_result_retention(owner=owner, parent_task_id=current["parent_task_id"], attempts={"A": 0}) == 1
        assert ad.get_delegation_result(unresolved, owner=owner)["result"] == result
        rows = ad.list_durable_delegations(owner=owner)
        assert len(rows) <= ad._MAX_RETAINED_COMPLETED
    assert ad.get_delegation_result(settled[0], owner=owner) is None


def test_in_memory_pruning_preserves_every_live_state(monkeypatch):
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 1)
    records = {state: {"status": state, "dispatched_at": 0} for state in ad._LIVE_STATES}
    records.update({f"done-{i}": {"status": "completed", "completed_at": i + 1} for i in range(3)})
    monkeypatch.setattr(ad, "_records", records)
    with ad._records_lock:
        ad._prune_completed_locked()
    assert set(records) == set(ad._LIVE_STATES) | {"done-2"}
