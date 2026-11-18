"""CVC selection must not interpret a customer identifier as a card secret target."""

import pytest

from agent.vault_login_classifier import (
    LoginControl,
    classify_checkout_control,
    select_checkout_fills,
)


@pytest.mark.parametrize("metadata,expected", [
    ({"name": "cid", "label": "Customer"}, False),
    # The DOM inspector joins [element.name, element.id] in this one slot.
    ({"name": "cid ", "label": "Customer"}, False),
    ({"name": " cid", "label": "Customer"}, False),
    ({"name": "cid", "label": "Customer ID"}, False),
    ({"name": "cid"}, False),
    ({"label": "Customer"}, False),
    ({"label": "Search"}, False),
    ({"label": "One-time code"}, False),
    ({"label": "CID"}, True),
    ({"name": "cid", "label": "CID"}, True),
    ({"name": "cid", "label": "Customer", "autocomplete": "cc-csc"}, True),
    ({"name": "cvc", "label": "Card security"}, True),
    ({"name": "cvv2"}, True),
    ({"label": "Security code"}, True),
    ({"label": "Card verification number"}, True),
])
@pytest.mark.parametrize("with_real_cvc", [False, True])
def test_cvc_target_requires_card_evidence(metadata, expected, with_real_cvc):
    control = LoginControl.from_dict({"index": 0, "type": "text", **metadata})
    classified = classify_checkout_control(control)
    assert (classified is not None and classified.token == "cc-csc") is expected
    controls = [classified] if classified else []
    if with_real_cvc:
        real = classify_checkout_control(LoginControl.from_dict(
            {"index": 1, "type": "password", "label": "Security code"}))
        assert real is not None
        controls.append(real)
    fills = select_checkout_fills(controls, {"cvc": "731"}, {"cvc": "cc-csc"})
    wanted = 0 if expected else 1 if with_real_cvc else None
    assert fills == ([] if wanted is None else [
        {"index": wanted, "token": "cc-csc", "value": "731"}])
