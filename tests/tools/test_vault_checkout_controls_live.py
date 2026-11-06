"""Synthetic checkout fixtures exercise real inspection, classification and DOM writes.

No merchant pages, personal browser, 1Password account or real card data are used.
"""
import json
from pathlib import Path

import pytest

from agent.vault_login_classifier import (
    LoginControl, build_fill_js, build_inspection_js,
    classify_checkout_control, select_checkout_fills,
)
from agent.vault_store import PAYMENT_FIELDS


@pytest.fixture
def page():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, args=['--use-mock-keychain'])
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip('Install the synthetic test browser: python -m playwright install chromium')
            raise
        with browser:
            yield browser.new_page()


def fill(page, html, secret):
    page.set_content(html)
    controls = json.loads(page.evaluate(build_inspection_js('checkout')))
    classified = [match for raw in controls
                  if (match := classify_checkout_control(LoginControl.from_dict(raw)))]
    fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS)
    result = page.evaluate(build_fill_js(fills, 'null', 'checkout'))
    assert secret['card_number'] not in result and secret['cvc'] not in result
    return json.loads(result)


@pytest.mark.parametrize('fixture', ['northwest', 'washington'])
def test_checkout_fixture_fills_complete_card_without_shifting_address(page, fixture):
    html = (Path(__file__).parents[1] / 'fixtures/browser_vault' / f'{fixture}_checkout.html').read_text()
    secret = {'card_number': '5555555555554444', 'cvc': '739', 'exp_month': '07',
              'exp_year': '2029', 'billing_postal_code': '98000'}
    result = fill(page, html, secret)
    assert result['filled'] == (5 if fixture == 'northwest' else 6)
    assert page.locator('[data-test=number]').input_value() == secret['card_number']
    assert page.locator('[data-test=cvc]').input_value() == secret['cvc']
    assert page.locator('[data-test=street]').input_value() == ''
    assert page.locator('[data-test=postal]').input_value() == '98000'
    if fixture == 'northwest':
        assert page.locator('[data-test=month]').inner_text() == '07'
        assert page.locator('[data-test=year]').inner_text() == '2029'
    else:
        assert page.locator('[data-test=month]').input_value() == '6'
        assert page.locator('[data-test=year]').input_value() == '29'
        assert page.locator('[data-test=brand]').input_value() == 'mc'
    assert page.locator('[data-hermes-vault-slot]').count() == 0


@pytest.mark.parametrize('metadata', [
    'name="CVVNumber"', 'id="cardCVC"', 'placeholder="CVV"',
    'aria-label="CSC"', 'placeholder="###" maxlength="3"',
])
def test_security_code_metadata_and_text_expiry_do_not_capture_authentication(page, metadata):
    html = f'''<input autocomplete="cc-number"><input data-test=cvc {metadata}>
      <input name="ExpirationDate" placeholder="MM / YYYY" maxlength="9">
      <input autocomplete="one-time-code" aria-label="Security code">
      <input type=password autocomplete="current-password" aria-label="Password">
      <input name="issueDate" placeholder="MM/YY"><input name="billingStreet">'''
    result = fill(page, html, {'card_number': '4111111111111111', 'cvc': '739',
                              'exp_month': '07', 'exp_year': '2029'})
    assert result['filled'] == 3
    assert page.locator('[data-test=cvc]').input_value() == '739'
    assert page.locator('[name=ExpirationDate]').input_value() == '07 / 2029'
    for selector in ('[autocomplete=one-time-code]', '[type=password]', '[name=issueDate]', '[name=billingStreet]'):
        assert page.locator(selector).input_value() == ''


@pytest.mark.parametrize('association', ['aria-controls=months', ''])
def test_custom_expiry_never_clicks_ambiguous_or_unassociated_options(page, association):
    html = f'''<input autocomplete=cc-number>
      <button type=button role=combobox {association} aria-label="Expiration month">MM</button>
      <div id=months role=listbox><button type=button role=option>07</button>
      <button type=button role=option>07</button></div>
      <script>window.optionClicks=0; for (const o of document.querySelectorAll('[role=option]'))
      o.onclick=()=>window.optionClicks++;</script>'''
    result = fill(page, html, {'card_number': '4111111111111111', 'cvc': '739',
                              'exp_month': '07', 'exp_year': '2029'})
    assert result['filled'] == 1
    assert page.evaluate('window.optionClicks') == 0


def test_camofox_async_secret_rejection_is_caught_before_server_logging(page, monkeypatch):
    from tools import browser_camofox as camofox
    from tools import browser_vault_tool as vault

    monkeypatch.setattr(camofox, 'is_camofox_mode', lambda: True)
    monkeypatch.setattr(camofox, 'get_camofox_url', lambda: 'http://127.0.0.1:9377')
    monkeypatch.setattr(camofox, '_get_session', lambda task: {'tab_id': 'fixture', 'user_id': 'fixture'})
    monkeypatch.setattr(camofox, '_post', lambda path, body, **kw: {'result': page.evaluate(body['expression'])})
    result = vault._eval_js_secret('fixture', 'Promise.reject(new Error("synthetic-secret"))')
    assert not result['success']
    assert 'synthetic-secret' not in json.dumps(result)
