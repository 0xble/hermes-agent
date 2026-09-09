"""Offline administrative dismissal must fence real startup without forging receipts."""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.delegation_cards import DelegationCards
from gateway.platforms.base import SendResult
from scripts.reconcile_delegation_cards import main


def fixture(tmp_path):
    owner = dict(profile="default", session_id="parent", session_key="route", chat_id="42", thread_id="8")
    cards = {}
    for ref, key, message in [("B", "b" * 32, "14"), ("C", "c" * 32, "15"), ("D", "d" * 32, "16")]:
        cards[key] = dict(owner=owner, source=dict(platform="telegram", chat_id="42", thread_id="8", message_id="source"),
                          started_at=1, generation=4, rows={ref: dict(thread_ref=ref, state="interrupted")},
                          message_id=message, rendered="old", recoveries=0, send_attempts=1, retired=False, handled=None)
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
async def test_explicit_dismissal_fences_startup_without_marking_results_handled(tmp_path):
    source, manifest, plan, original = fixture(tmp_path)
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
    assert {call.args[1] for call in adapter.edit_message.await_args_list} == {"14", "15", "16"}
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
    adapter.send_delegation_card.assert_awaited_once()  # failed-send sibling survives
    persisted = json.loads(path.read_bytes())
    assert persisted["b" * 32]["presentation_dismissal"] == result["b" * 32]["presentation_dismissal"]
    assert not persisted["d" * 32]["retired"]


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
