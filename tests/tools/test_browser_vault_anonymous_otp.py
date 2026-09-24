"""Real-DOM regression for anonymous split OTP fields (GoHighLevel shape)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.vault_login_classifier import (
    LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/browser_vault/ghl_split_otp.html"


def _controls(page):
    inspected = page.evaluate(build_inspection_js("fixture-nonce"))
    assert "value" not in inspected  # inspection exposes only an empty boolean
    return [LoginControl.from_dict(raw) for raw in json.loads(inspected)]


def test_ghl_anonymous_boxes_through_real_browser_and_secure_prompt():
    playwright = pytest.importorskip("playwright.sync_api", reason="real-browser fixture needs Playwright")
    try:
        with playwright.sync_playwright() as p, p.chromium.launch(headless=True) as browser:
            page = browser.new_page()
            page.goto(FIXTURE.as_uri())
            controls = _controls(page)
            classified = classify_otp_controls(controls)
            assert len(classified) == 6
            assert all(c.token == "anonymous-one-time-code" for c in classified)
            assert all(c.control.empty and c.control.type == "number" for c in classified)
            assert [f["value"] for f in build_otp_fills(classified, "246810")] == list("246810")
            assert build_otp_fills(classified, "24681") == []

            from agent.vault_backends import unlock
            from tools import browser_vault_tool
            prompted = []
            unlock.set_code_prompt_callback(lambda site, hint: prompted.append(site) or "246810")
            origin = page.evaluate("window.location.origin")
            def evaluate(_task, expression):
                return {"success": True, "result": page.evaluate(expression)}
            try:
                with patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
                     patch.object(browser_vault_tool, "_current_page_origin", return_value=origin), \
                     patch.object(browser_vault_tool, "_eval_js", side_effect=evaluate), \
                     patch.object(browser_vault_tool, "_eval_js_secret", side_effect=evaluate), \
                     patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
                    outcome = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="fixture"))
            finally:
                unlock.set_code_prompt_callback(None)
            assert outcome["success"] and outcome["filled_fields"] == 6
            assert prompted and "246810" not in json.dumps(outcome)
            assert page.locator("#digit-row input").evaluate_all("els => els.map(el => el.value)") == list("246810")
            assert page.locator("[data-hermes-vault-slot]").count() == 0
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium not installed in this environment")
        raise


@pytest.mark.parametrize("variant", [
    "card", "phone", "date", "address", "coupon", "pin", "nonadjacent", "unequal", "password", "filled", "type", "count", "disabled", "readonly", "hidden",
])
def test_ghl_shape_rejects_unsafe_variants_in_real_dom(variant):
    playwright = pytest.importorskip("playwright.sync_api", reason="real-browser fixture needs Playwright")
    try:
        with playwright.sync_playwright() as p, p.chromium.launch(headless=True) as browser:
            page = browser.new_page()
            page.goto(FIXTURE.as_uri())
            mutations = {
                "card": "document.querySelector('h2').textContent = 'Card security code CVV'",
                "phone": "document.querySelector('h2').textContent = 'Phone verification'",
                "date": "document.querySelector('h2').textContent = 'Date verification MM/YY'",
                "address": "document.querySelector('h2').textContent = 'Address ZIP verification'",
                "coupon": "document.querySelector('h2').textContent = 'Promo voucher verification'",
                "pin": "document.querySelector('h2').textContent = 'Enter PIN'; document.querySelectorAll('label')[1].textContent = 'Choose email instead'",
                "nonadjacent": "document.querySelector('#digit-row input').after(Object.assign(document.createElement('input'), {type:'text', name:'other'}))",
                "unequal": "document.querySelector('#digit-row input').style.width = '60px'",
                "password": "document.querySelector('#digit-row').append(Object.assign(document.createElement('input'), {type:'password'}))",
                "filled": "document.querySelector('#digit-row input').value = '3'",
                "type": "document.querySelector('#digit-row input').type = 'tel'",
                "count": "document.querySelectorAll('#digit-row input').forEach((el, i) => { if (i >= 3) el.remove() })",
                "disabled": "document.querySelector('#digit-row input').disabled = true",
                "readonly": "document.querySelector('#digit-row input').readOnly = true",
                "hidden": "document.querySelector('#digit-row input').style.display = 'none'",
            }
            page.evaluate(mutations[variant])
            classified = classify_otp_controls(_controls(page))
            assert not classified, variant
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium not installed in this environment")
        raise


def test_anonymous_classifier_guard_matrix_and_code_length():
    base = dict(autocomplete="", form_index=0, label="", name="", type="number", empty=True,
                width=40, height=40, container_index=1, container_input_count=6,
                nearby_text="Verify Security Code Use Authenticator App")
    boxes = [LoginControl(index=i, **base) for i in range(6)]
    assert len(classify_otp_controls(boxes)) == 6
    assert build_otp_fills(classify_otp_controls(boxes), "12345") == []
    assert [x["value"] for x in build_otp_fills(classify_otp_controls(boxes), "123456")] == list("123456")
    for change in (
        {"nearby_text": "card security code"}, {"nearby_text": "Phone verification"},
        {"nearby_text": "PIN"}, {"context_truncated": True}, {"empty": False},
        {"container_has_password": True}, {"width": 60}, {"name": "segment"},
        {"container_index": 2}, {"form_index": 1}, {"type": "password"},
        {"container_input_count": 7}, {"index": 10},
    ):
        modified = [*boxes[:-1], LoginControl(**{**boxes[-1].__dict__, **change})]
        assert not classify_otp_controls(modified), change
    explicit = LoginControl(index=10, autocomplete="one-time-code", form_index=0,
                            label="", name="", type="text")
    assert [x.control.index for x in classify_otp_controls([*boxes, explicit])] == [10]
