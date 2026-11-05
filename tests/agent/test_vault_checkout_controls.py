"""Payment metadata regression tests; no browser or real card data required."""
import pytest

from agent.vault_login_classifier import LoginControl, classify_checkout_control, select_checkout_fills
from agent.vault_store import PAYMENT_FIELDS


def control(index=0, **kwargs):
    return LoginControl.from_dict({'index': index, 'formIndex': 0, **kwargs})


@pytest.mark.parametrize('name,label,token', [
    ('CVVNumber', '', 'cc-csc'), ('cardCVC', '', 'cc-csc'), ('', 'CSC', 'cc-csc'),
    ('', '###', 'cc-csc'), ('', 'CVV', 'cc-csc'), ('', 'Security code', 'cc-csc'),
    ('ExpirationMonth', '', 'cc-exp-month'), ('ExpirationYear', '', 'cc-exp-year'),
    ('CardType', '', 'cc-type'), ('', 'MM / YYYY', 'cc-exp'),
])
def test_framework_payment_metadata(name, label, token):
    match = classify_checkout_control(control(0, name=name, label=label, type='text'))
    assert match and match.token == token


@pytest.mark.parametrize('autocomplete', ['one-time-code', 'current-password', 'new-password', 'username'])
def test_authentication_metadata_does_not_capture_cvc(autocomplete):
    assert classify_checkout_control(control(0, name='CVVNumber', label='Security code',
                                                type='text', autocomplete=autocomplete)) is None


@pytest.mark.parametrize('number,brand', [
    ('4111111111111111', 'visa'), ('5555555555554444', 'mastercard'),
    ('2221000000000009', 'mastercard'), ('378282246310005', 'amex'),
    ('6011111111111117', 'discover'), ('not-a-card', None),
])
def test_card_brand_is_derived_server_side(number, brand):
    match = classify_checkout_control(control(0, name='CardType', type='select'))
    assert match is not None
    fills = select_checkout_fills([match], {'card_number': number}, PAYMENT_FIELDS)
    assert fills == ([{'index': 0, 'token': 'cc-type', 'value': brand}] if brand else [])


def test_expiry_text_width_and_address_tokens_stay_distinct():
    controls = [control(0, name='ExpirationYear', type='text', maxLength=2),
                control(1, name='BillingAddress', type='text'),
                control(2, name='ZipCode', type='text')]
    classified = [match for c in controls if (match := classify_checkout_control(c))]
    fills = select_checkout_fills(classified, {'exp_year': '2029', 'billing_postal_code': '98000'}, PAYMENT_FIELDS)
    assert fills == [{'index': 0, 'token': 'cc-exp-year', 'value': '29'},
                     {'index': 2, 'token': 'postal-code', 'value': '98000'}]
