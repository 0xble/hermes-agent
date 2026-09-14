"""Offline administrative dismissal must fence real startup without forging receipts."""
import asyncio
import copy
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.delegation_cards import DelegationCards
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from scripts.reconcile_delegation_cards import main


def fixture(tmp_path, batch=None):
    owner = dict(profile="default", session_id="parent", session_key="route", chat_id="42", thread_id="8")
    cards = {}
    for ref, key, message in [("B", "b" * 32, "14"), ("C", "c" * 32, "15"), ("D", "d" * 32, "16")]:
        cards[key] = dict(owner=owner, source=dict(platform="telegram", chat_id="42", thread_id="8", message_id="source"),
                          started_at=1, generation=4, rows={ref: dict(
                              thread_ref=ref, state="interrupted", terminal_at=time.time() - 1,
                              display_expires_at=time.time() + 300)},
                          message_id=message, rendered="old", recoveries=0, send_attempts=1, retired=False, handled=None)
        if batch and ref in {"B", "C"}:
            cards[key]["rows"][ref]["original_call_id"] = key
            cards[key]["original_calls"] = {key: {"manifest": dict(
                id=key, parent_task_id=key,
                member_refs=[ref, "Z"] if batch == "incomplete" else [ref])}}
    # Unsent failed transport remains retryable; it is not part of the dismissal.
    cards["e" * 32] = copy.deepcopy(cards["d" * 32])
    cards["e" * 32].update(message_id=None, send_attempts=0)
    cards["e" * 32]["rows"]["D"]["state"] = "failed"
    raw = json.dumps(cards).encode()
    source = tmp_path / "snapshot.json"
    source.write_bytes(raw)
    plan = dict(schema="delegation-card-dismissal-v1", snapshot_sha256=hashlib.sha256(raw).hexdigest(),
                operator="test-operator", authorization="explicit exact-target operator approval", targets=[])
    for key in ("b" * 32, "c" * 32):
        card = cards[key]
        plan["targets"].append(dict(parent_task_id=key, owner=owner, source=card["source"],
                                    refs=list(card["rows"]), message_id=card["message_id"],
                                    reason="superseded", evidence="parent transcript record 123; operator confirmed this exact task"))
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    return source, manifest, plan, cards


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [None, "complete", "incomplete"])
async def test_explicit_dismissal_fences_startup_without_marking_results_handled(tmp_path, batch):
    source, manifest, plan, original = fixture(tmp_path, batch)
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="new")),
                              edit_message=AsyncMock(return_value=SendResult(success=True)),
                              delete_message=AsyncMock(return_value=False))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    home = tmp_path / "profile"
    path = home / "cache" / "delegation" / "cards.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(source.read_bytes())
    baseline = DelegationCards(runner, home=home, interval=0)
    await baseline.reconcile()
    await asyncio.gather(*list(baseline.pending.values()))
    assert {call.args[1] for call in adapter.edit_message.await_args_list} == {"14"}
    text = adapter.edit_message.call_args.args[2]
    assert text.count("Ⅱ Task") == 3 and text.count("! Task") == 1
    assert all(ref not in text for ref in ("B.", "C.", "D.", "D·2."))
    assert set(baseline.cards["b" * 32]["rows"]) == {"B"}
    for mock in (adapter.send_delegation_card, adapter.edit_message, adapter.delete_message):
        mock.reset_mock()
    output = tmp_path / "reconciled.json"
    args = ["--snapshot", str(source), "--manifest", str(manifest), "--output", str(output)]
    assert main(args) == 0
    assert not output.exists()  # default is validation only
    assert main([*args, "--write-candidate"]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    for target in plan["targets"]:
        card = result[target["parent_task_id"]]
        assert card["retired"] and card["handled"] is None
        assert card["rows"] == original[target["parent_task_id"]]["rows"]
        assert card["presentation_dismissal"]["target"] == target
        assert card["presentation_dismissal"]["authorization"] == plan["authorization"]
    assert result["d" * 32] == original["d" * 32]
    assert result["e" * 32] == original["e" * 32]
    assert json.loads(source.read_bytes()) == original
    path.write_bytes(output.read_bytes())  # disposable-only installation rehearsal
    restored = DelegationCards(runner, home=home, interval=0)
    await restored.reconcile()
    await asyncio.gather(*list(restored.pending.values()))
    assert [call.args[1] for call in adapter.edit_message.await_args_list] == ["16"]
    assert {call.args[1] for call in adapter.delete_message.await_args_list} == {"14", "15"}
    adapter.send_delegation_card.assert_not_awaited()  # failed-send sibling joins existing message
    assert "! Task" in adapter.edit_message.call_args.args[2]
    assert set(restored.cards["e" * 32]["rows"]) == {"D"}
    persisted = json.loads(path.read_bytes())
    assert persisted["b" * 32]["presentation_dismissal"] == result["b" * 32]["presentation_dismissal"]
    assert not persisted["d" * 32]["retired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_anchor", [False, True])
async def test_batch_dismissal_fences_callbacks_and_preserves_live_descendant(tmp_path, monkeypatch, shared_anchor):
    _, _, plan, cards = fixture(tmp_path, "incomplete")
    key, sibling = "b" * 32, "d" * 32
    cards.pop("c" * 32)
    cards.pop("e" * 32)
    plan["targets"] = plan["targets"][:1]
    cards[key]["rows"]["B"].update(child_session_id="child", attempt=0)
    cards[sibling]["rows"]["D"].update(
        card_parent_task_id=key, card_parent_thread_ref="B", task_label="Live descendant", state="running")
    if shared_anchor:
        cards[sibling].update(presentation_key=key, message_id=None)
    raw = json.dumps(cards)
    plan["snapshot_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    directory = tmp_path / "cache" / "delegation"
    directory.mkdir(parents=True)
    (directory / "cards.json").write_text(raw)
    (directory / "dismissal-request.json").write_text(json.dumps(dict(snapshot_json=raw, manifest=plan)))
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="new")),
                              edit_message=AsyncMock(return_value=SendResult(success=True)),
                              delete_message=AsyncMock(return_value=True))
    monkeypatch.setattr("gateway.delivery_ledger.delivered_delegation_receipts", lambda: [])
    releases = []
    monkeypatch.setattr("tools.async_delegation.release_result_retention", lambda **kw: releases.append(kw))
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)

    async def drain():
        for _ in range(30):
            pending = list(manager.pending.values())
            if not pending:
                return
            await asyncio.gather(*pending)
        pytest.fail("presentation failed to settle")

    try:
        await manager.reconcile()
        await drain()
        anchor = manager._anchor(sibling)
        assert list(manager._display_projection(anchor)["rows"]) == [sibling + ":D"]
        assert {call.args[1] for call in adapter.edit_message.await_args_list} == ({"14"} if shared_anchor else {"16"})
        assert {call.args[1] for call in adapter.delete_message.await_args_list} == (set() if shared_anchor else {"14"})
        # Shared binding may assign display refs, but cannot change execution fields.
        for ref, original_row in cards[key]["rows"].items():
            assert {field: manager.cards[key]["rows"][ref][field] for field in original_row} == original_row
        assert manager.cards[key]["handled"] is None
        assert manager.cards[key]["presentation_dismissal"]["target"] == plan["targets"][0]
        before = copy.deepcopy(manager.cards[key])
        for mock in (adapter.send_delegation_card, adapter.edit_message, adapter.delete_message):
            mock.reset_mock()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
        common = dict(owner=cards[key]["owner"], parent_task_id=key)
        callbacks = [
            ("start", dict(thread_ref="Z", original_call=cards[key]["original_calls"][key]["manifest"])),
            ("complete", dict(thread_ref="B", status="completed", attempt=0)),
            ("admitted", dict(thread_ref="B", child_session_id="child", attempt=1, resume_claim_id="new-claim")),
        ]
        for kind, data in callbacks:
            await manager.observe(source, "route", "parent", 1, "subagent." + kind, None, {**common, **data})
        await drain()
        assert manager.cards[key] == before
        assert releases == []
        assert list(manager._display_projection(anchor)["rows"]) == [sibling + ":D"]
        for mock in (adapter.send_delegation_card, adapter.edit_message, adapter.delete_message):
            mock.assert_not_awaited()
    finally:
        await manager.shutdown()
        await drain()


def test_dismissal_rejects_drift_partial_identity_active_rows_and_output_overwrite(tmp_path):
    source, manifest, plan, original = fixture(tmp_path)
    output = tmp_path / "candidate.json"
    args = ["--snapshot", str(source), "--manifest", str(manifest), "--output", str(output), "--write-candidate"]
    changes = [lambda p: p.update(authorization=""), lambda p: p.update(snapshot_sha256="0" * 64),
               lambda p: p["targets"][0].update(refs=[]), lambda p: p["targets"][1].update(message_id="wrong"),
               lambda p: p["targets"][0].update(owner={}), lambda p: p["targets"].append(p["targets"][0]),
               lambda p: p["targets"][0].update(evidence="")]
    for change in changes:
        bad = copy.deepcopy(plan)
        change(bad)
        manifest.write_text(json.dumps(bad), encoding="utf-8")
        assert main(args) == 2
        assert not output.exists()
        assert json.loads(source.read_bytes()) == original
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    assert main([*args[:-3], "--output", str(source), "--write-candidate"]) == 2
    output.write_text("preserve", encoding="utf-8")
    assert main(args) == 2
    assert output.read_text(encoding="utf-8") == "preserve"
    output.unlink()
    original["b" * 32]["rows"]["B"]["state"] = "running"
    raw = json.dumps(original).encode()
    source.write_bytes(raw)
    plan["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest.write_text(json.dumps(plan), encoding="utf-8")
    assert main(args) == 2
    assert not output.exists()


@pytest.mark.parametrize("change", [None, "rows", "message_id", "owner", "handled"])
def test_startup_request_preserves_live_anchor_and_unrelated_work(tmp_path, change):
    source, manifest, plan, original = fixture(tmp_path)
    home = tmp_path / "profile"
    directory = home / "cache" / "delegation"
    directory.mkdir(parents=True)
    request = {"snapshot_json": source.read_text(), "manifest": plan}
    (directory / "dismissal-request.json").write_text(json.dumps(request))
    live = copy.deepcopy(original)
    # A terminal row's physical message hosts unrelated newer work. Its visual
    # revision changing is not a different task/outcome/transport identity.
    live["b" * 32].update(rendered="new active sibling", revision=90)
    live["d" * 32]["rows"]["D"]["last_tool"] = "terminal"
    if change == "rows":
        live["b" * 32]["rows"]["B"]["state"] = "failed"
    elif change:
        live["b" * 32][change] = {"changed": True} if change == "owner" else "different"
    (directory / "cards.json").write_text(json.dumps(live))
    manager = DelegationCards(SimpleNamespace(), home=home)
    for key in ("b" * 32, "c" * 32):
        assert manager.cards[key]["retired"] is (change is None)
        assert manager.cards[key]["handled"] == live[key]["handled"]
        assert manager.cards[key]["rows"] == live[key]["rows"]
    assert manager.cards["d" * 32]["rows"] == live["d" * 32]["rows"]
    assert manager.cards["b" * 32]["rendered"] == "new active sibling"
    if not change:
        assert not (directory / "dismissal-request.json").exists()
        # A replay after persistence but before archival is idempotent.
        (directory / "dismissal-request.json").write_text(json.dumps(request))
        again = DelegationCards(SimpleNamespace(), home=home)
        assert again.cards["b" * 32]["presentation_dismissal"] == manager.cards["b" * 32]["presentation_dismissal"]
        assert not (directory / "dismissal-request.json").exists()
