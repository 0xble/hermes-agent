"""Opt-in local Camoufox fixture; never connects to a checkout or user profile.

VAULT_BROWSER_FIXTURE_MODULES points to installed playwright-core/camoufox-js;
VAULT_BROWSER_FIXTURE_SOURCE to Camofox companion PR54. All returned checks are
booleans/field tokens. Ordinary unit tests do not require a browser download.
"""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.vault_login_classifier import LoginControl, build_inspection_js, classify_checkout_control
from tools import browser_vault_payment as payment

CARD = {"card_number": "4111111111111111", "exp_month": "07", "exp_year": "2029", "cvc": "9876"}


@pytest.fixture(scope="module")
def bridge():
    if not os.environ.get("VAULT_BROWSER_FIXTURE_MODULES") or not os.environ.get("VAULT_BROWSER_FIXTURE_SOURCE"):
        pytest.skip("opt-in isolated native-browser fixture")
    process = subprocess.Popen(["node", str(Path(__file__).parents[1] / "fixtures/vault_checkout_bridge.cjs")],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = json.loads(process.stdout.readline())
    assert ready["ready"]
    def call(op, **kwargs):
        process.stdin.write(json.dumps({"op": op, **kwargs}) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())
    try:
        yield ready, call
    finally:
        process.stdin.close()
        process.wait(timeout=20)
        assert process.returncode == 0, process.stderr.read()


def inspected(call, origin, frame_id=None):
    response = call("frame" if frame_id else "eval", expression=build_inspection_js("synthetic"), **({"frameId": frame_id} if frame_id else {}))
    assert response["ok"]
    raw = json.loads(response["result"])
    assert all(value not in json.dumps(raw) for value in CARD.values())
    controls = [LoginControl.from_dict(c) for c in raw]
    return {"origin": origin, "nonce": "synthetic", "controls": controls,
            "classified": [c for control in controls if (c := classify_checkout_control(control))],
            **({"frame_id": frame_id} if frame_id else {})}


@pytest.mark.parametrize("kind", ["native", "aria", "shadow", "partial", "duplicate", "frame"])
def test_isolated_checkout_matrix(bridge, kind):
    ready, call = bridge
    assert call("load", kind=kind)["ok"]
    call("events")
    if kind == "frame":
        discovered = call("discover")["result"]
        assert len(discovered) == 1
        contexts = [inspected(call, ready["processorOrigin"], discovered[0]["frame_id"])]
    else:
        contexts = [inspected(call, ready["origin"])]
    selected, ambiguous = payment.plan(contexts, CARD)
    def evaluate(task, expression):
        response = call("eval", expression=expression)
        return {"success": response["ok"], "result": response.get("result")}
    def frame_evaluate(task, frame_id, expression, *args, **kwargs):
        response = call("frame", frameId=frame_id, expression=expression)
        return {"success": response["ok"], "result": response.get("result")}
    with patch.object(payment, "evaluate_payment_frame", side_effect=frame_evaluate):
        result = payment.fill("fixture", ready["origin"], selected, ambiguous, evaluate)
    assert all(value not in json.dumps(result) for value in CARD.values())
    check = call("check")["result"]
    if kind in {"partial", "duplicate"}:
        assert not result["success"] and result["partial"]
        assert check["cvc"] is False
        if kind == "duplicate":
            assert check["pan"] is False
            assert result["field_results"]["cc-csc"] == "ambiguous"
    else:
        assert result["success"] and result["partial"] is False, result
        assert check["pan"] and check["cvc"] and check["expiry"] and check["promoEmpty"]
        if kind in {"native", "aria"}:
            assert check["events"] >= 2


def test_real_frame_navigation_revokes_document(bridge):
    ready, call = bridge
    call("load", kind="frame")
    frame_id = call("discover")["result"][0]["frame_id"]
    context = inspected(call, ready["processorOrigin"], frame_id)
    call("navigate-frame")
    selected, ambiguous = payment.plan([context], CARD)
    def evaluate_frame(task, frame_id, expression, *a, **kw):
        response = call("frame", frameId=frame_id, expression=expression)
        return {"success": response["ok"], "result": response.get("result")}
    with patch.object(payment, "evaluate_payment_frame", side_effect=evaluate_frame):
        result = payment.fill("fixture", ready["origin"], selected, ambiguous, lambda *a: pytest.fail("page fallback"))
    assert not result["success"] and result["partial"] and result["filled_fields"] == 0
    check = call("check")["result"]
    assert not check["pan"] and not check["cvc"]
