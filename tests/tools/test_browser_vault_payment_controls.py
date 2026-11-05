"""Value-blind payment planning and mutation receipts (synthetic cards only)."""
import json
from unittest.mock import Mock

import pytest

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from tools.browser_vault_payment import fill, plan, receipt

CARD = {"card_number": "4111111111111111", "exp_month": "07", "exp_year": "2029", "cvc": "9876"}
ORIGIN = "https://merchant.test"


def context(*raw, frame_id=None):
    controls = [LoginControl.from_dict({"index": i, "type": "text", **c}) for i, c in enumerate(raw)]
    return {"origin": ORIGIN, "nonce": "fixture", "controls": controls,
            "classified": [c for control in controls if (c := classify_checkout_control(control))],
            **({"frame_id": frame_id} if frame_id else {})}


def test_adjacent_hash_requires_unique_local_cvc_label():
    control = context({"label": "###", "groupLabel": "CVC", "groupCandidateCount": 1})
    assert [c.token for c in control["classified"]] == ["cc-csc"]
    assert not context({"label": "###"})["classified"]
    assert not context({"label": "###", "groupLabel": "Promo code", "groupCandidateCount": 1})["classified"]
    assert not context({"label": "###", "groupLabel": "CVC", "groupCandidateCount": 2})["classified"]


def test_aria_expiry_uses_accessible_local_group():
    assert context({"type": "combobox", "label": "Month", "groupLabel": "Expiration"})["classified"][0].token == "cc-exp-month"
    assert context({"type": "listbox", "label": "Year", "groupLabel": "Expiry"})["classified"][0].token == "cc-exp-year"


def test_duplicate_sensitive_targets_refuses_all_writes():
    ctx = context({"autocomplete": "cc-number"}, {"label": "CVC"}, {"label": "CVC"})
    selected, ambiguous = plan([ctx], CARD)
    writer = Mock()
    result = fill("task", ORIGIN, selected, ambiguous, writer)
    assert result["success"] is False and result["partial"] is True
    assert result["field_results"]["cc-csc"] == "ambiguous"
    writer.assert_not_called()


def test_pan_and_postal_count_is_not_complete_checkout():
    ctx = context({"autocomplete": "cc-number"}, {"autocomplete": "postal-code"})
    selected, ambiguous = plan([ctx], {**CARD, "billing_postal_code": "94110"})
    writer = Mock(return_value={"success": True, "result": json.dumps({"filled": 2, "fieldResults": [
        {"token": "cc-number", "status": "filled"}, {"token": "postal-code", "status": "filled"}]})})
    result = fill("task", ORIGIN, selected, ambiguous, writer)
    assert result["success"] is False and result["partial"] is True
    assert result["missing_fields"] == ["cc-csc", "cc-exp-month", "cc-exp-year"]
    assert result["field_results"]["cc-number"] == "filled"
    assert all(secret not in json.dumps(result) for secret in CARD.values())


def test_count_only_is_not_a_field_readback():
    selected, ambiguous = plan([context({"autocomplete": "cc-number"}, {"autocomplete": "cc-exp"}, {"label": "CVC"})], CARD)
    result = fill("task", ORIGIN, selected, ambiguous, Mock(return_value={"success": True, "result": {"filled": 3}}))
    assert not result["success"] and result["filled_fields"] == 0


def test_combined_expiry_receipt_covers_both_parts():
    result = receipt({"cc-number", "cc-exp", "cc-csc"}, set(), 3)
    assert result["success"] and result["partial"] is False
    assert result["missing_fields"] == []


def test_frame_navigation_retains_truthful_prior_field_receipts(monkeypatch):
    page = context({"autocomplete": "cc-number"})
    frame = context({"autocomplete": "cc-exp"}, {"label": "CVC"}, frame_id="bound-document")
    frame["origin"] = "https://js.stripe.com"
    selected, ambiguous = plan([page, frame], CARD)
    monkeypatch.setattr("tools.browser_vault_payment.evaluate_payment_frame", lambda *a, **k: {"success": False})
    writer = Mock(return_value={"success": True, "result": {"fieldResults": [{"token": "cc-number", "status": "filled"}]}})
    result = fill("task", ORIGIN, selected, ambiguous, writer)
    assert result["partial"] and not result["success"]
    assert result["filled_fields"] == 1 and result["fields"] == ["cc-number"]
    assert "cc-csc" in result["missing_fields"]


def test_receipt_uses_final_pass_not_historical_write_results():
    ctx = context({'autocomplete': 'cc-number'}, {'autocomplete': 'cc-exp'}, {'label': 'CVC'})
    selected, ambiguous = plan([ctx], CARD)
    initial = {'fieldResults': [{'token': f['token'], 'status': 'filled'} for f in selected[0]['fills']]}
    final = {'fieldResults': [{'token': f['token'], 'status': 'missing' if f['token'] == 'cc-number' else 'filled'} for f in selected[0]['fills']]}
    writer = Mock(side_effect=[{'success': True, 'result': initial}, {'success': True, 'result': final}])
    result = fill('task', ORIGIN, selected, ambiguous, writer)
    assert not result['success'] and result['field_results']['cc-number'] == 'missing'
    assert writer.call_count == 2
    assert 'const verifyOnly = true' in writer.call_args_list[-1].args[1]
