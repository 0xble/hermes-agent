"""Lost validation replies reconcile only the caller's pre-launch reservation."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from gateway.delegation_cards import DelegationCards
from tests.gateway.test_delegation_handling import setup, drain
from tests.tools.test_delegate_required_labels import _valid_runtime
from tools import delegate_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_lost_validation_receipt_cancels_exact_request_before_retry(tmp_path, monkeypatch, committed):
    cards, source, _, data, parent = await setup(tmp_path, monkeypatch)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "failed"})
    await drain(cards)
    _valid_runtime(monkeypatch)
    monkeypatch.setattr(delegate_tool, "_capture_origin", lambda: (None, None, None, None, False))
    parent._delegate_depth = 0
    loop = asyncio.get_running_loop()
    requests = []

    def callback(event, **kwargs):
        nonlocal cards
        if event != "subagent.handling":
            return None
        if kwargs["reason"] == "validate_replacement":
            requests.append(dict(kwargs))
            if len(requests) == 1:
                if committed:
                    asyncio.run_coroutine_threadsafe(cards.handling(source, "r", "s", 2, **kwargs), loop).result(5)
                cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
                raise TimeoutError("validation acknowledgement lost")
        return asyncio.run_coroutine_threadsafe(cards.handling(source, "r", "s", 2, **kwargs), loop).result(5)

    parent.tool_progress_callback = callback
    child = SimpleNamespace(_progress_identity_ref={}, _delegate_role="leaf")
    builds, launches = [], []

    def build(tasks, *args, **kwargs):
        builds.append(tasks)
        return [(0, tasks[0], child)], None

    def run(batch, background):
        launches.append(batch)
        raise TimeoutError("actual launch outcome unresolved")

    monkeypatch.setattr(delegate_tool, "_build_children", build)
    monkeypatch.setattr(delegate_tool, "_run_batch", run)
    tasks = [{"goal": "Replace failed work", "task_label": "Retry work", "replaces": {
        "parent_task_id": data["parent_task_id"], "thread_ref": "A"}}]
    failed = json.loads(await asyncio.to_thread(delegate_tool.delegate_task, parent_agent=parent, tasks=deepcopy(tasks)))
    assert "acknowledgement lost" in failed["error"] and not builds and not launches
    cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
    assert not cards.cards[data["parent_task_id"]].get("replacement_claims")
    # The original callback can still be queued AFTER cancellation and reload.
    with pytest.raises(ValueError, match="cancelled"):
        await cards.handling(source, "r", "s", 2, **requests[0])
    with pytest.raises(TimeoutError, match="actual launch"):
        await asyncio.to_thread(delegate_tool.delegate_task, parent_agent=parent, tasks=deepcopy(tasks))
    assert len(builds) == len(launches) == 1
    assert requests[0]["detail"] != requests[1]["detail"]
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    claim = restored.cards[data["parent_task_id"]]["replacement_claims"]["A"]
    assert claim["id"] == child._progress_identity_ref["replaces"]["claim_id"]
    assert not restored.cards[data["parent_task_id"]].get("handled")
    # A lost dispatch reply never enters validation cleanup or retries launch.
    for request in [requests[1], {**requests[1], "detail": "another-caller"}]:
        with pytest.raises(ValueError, match="reserved|unresolved"):
            await restored.handling(source, "r", "s", 2, **request)
    result = json.loads(await asyncio.to_thread(delegate_tool.delegate_task, parent_agent=parent, tasks=deepcopy(tasks)))
    assert "reserved" in result["error"] and len(launches) == 1
    # A delayed cancellation of the first request cannot release the new claim.
    await restored.handling(source, "r", "s", 2, **{**requests[0], "reason": "release_replacement"})
    assert restored.cards[data["parent_task_id"]]["replacement_claims"]["A"] == claim
    # Canonical admission may later resolve dispatch. Duplicate admission still
    # links one child row and retires the original once, not on validation.
    admitted = {**child._progress_identity_ref, "owner": data["owner"], "card_owner": data["owner"]}
    await restored.observe(source, "r", "s", 2, "subagent.start", None, admitted)
    for _ in range(2):
        await restored.observe(source, "r", "s", 2, "subagent.admitted", None, admitted)
    await drain(restored)
    final = DelegationCards(cards.runner, home=tmp_path, interval=0)
    old = final.cards[data["parent_task_id"]]
    assert old["handled"] == ["A"]
    assert old["replacement_claims"]["A"]["launched"] == {
        "parent_task_id": admitted["parent_task_id"], "thread_ref": admitted["thread_ref"]}
    assert list(final.cards[admitted["parent_task_id"]]["rows"]) == [admitted["thread_ref"]]
    await final.handling(source, "r", "s", 2, **{**requests[1], "reason": "release_replacement"})
    assert final.cards[data["parent_task_id"]]["replacement_claims"]["A"]["launched"]
    await drain(cards)


@pytest.mark.asyncio
async def test_exact_cancellation_preserves_owner_attempt_and_ambiguous_claim_fences(tmp_path, monkeypatch):
    cards, source, _, data, _ = await setup(tmp_path, monkeypatch)
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "failed"})
    await drain(cards)
    request = dict(actor_session_id="s", parent_task_id=data["parent_task_id"], refs=["A"],
                   reason="validate_replacement", detail="caller-reservation")
    first = await cards.handling(source, "r", "s", 2, **request)
    cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
    for detail in (None, request["detail"], "another-caller"):
        with pytest.raises(ValueError, match="reserved"):
            await cards.handling(source, "r", "s", 2, **{**request, "detail": detail})
    for reason in ("validate_replacement", "release_replacement"):
        with pytest.raises(ValueError, match="exact parent"):
            await cards.handling(source, "r", "s", 2, **{**request, "reason": reason, "actor_session_id": "other"})
    cards.cards[data["parent_task_id"]]["rows"]["A"]["attempt"] = 1
    with pytest.raises(ValueError, match="reserved"):
        await cards.handling(source, "r", "s", 2, **request)
    await cards.handling(source, "r", "s", 2, **{**request, "reason": "release_replacement", "detail": "another-caller"})
    assert cards.cards[data["parent_task_id"]]["replacement_claims"]["A"]["id"] == first["claim_id"]
    await cards.handling(source, "r", "s", 2, **{**request, "reason": "release_replacement"})
    cards = DelegationCards(cards.runner, home=tmp_path, interval=0)
    with pytest.raises(ValueError, match="cancelled"):
        await cards.handling(source, "r", "s", 2, **request)
    fresh = await cards.handling(source, "r", "s", 2, **{**request, "detail": "fresh-caller"})
    assert fresh["validated"] and fresh["attempt"] == 1
    await cards.handling(source, "r", "s", 2, **{**request, "reason": "release_replacement"})
    assert cards.cards[data["parent_task_id"]]["replacement_claims"]["A"]["id"] == fresh["claim_id"]
