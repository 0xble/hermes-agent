"""A filled card number stays redacted when the page shows it with different separators.

Checkout inputs reformat a card number as it is typed (``4111 1111 1111 1111``, NBSPs, dashes) and a
password manager may store it formatted, so the exact bytes registered at fill time are often not what a
later browser read returns. Card numbers here are standard test numbers.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent import redact
from agent.vault_store import VaultStore, normalize_origin

_REDACTED = "«redacted-vault-secret»"


@pytest.fixture(autouse=True)
def _clean_registry():
    redact.clear_vault_redaction_values()
    yield
    redact.clear_vault_redaction_values()


@pytest.mark.parametrize("shown", [
    "4111 1111 1111 1111",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111 - 1111 - 1111 - 1111",
    "4111111111111111",
], ids=["spaces", "nbsp", "dashes", "spaced-dashes", "digits"])
@pytest.mark.parametrize("stored", ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111"],
                         ids=["stored-digits", "stored-spaces", "stored-dashes"])
def test_a_registered_card_number_is_redacted_however_the_page_formats_it(stored, shown):
    redact.register_vault_card_number(stored)
    out = redact.redact_registered_vault_values(f'{{"value": "{shown}", "name": "card"}}')
    assert out == f'{{"value": "{_REDACTED}", "name": "card"}}'


def test_another_card_number_and_partial_digit_runs_are_left_alone():
    redact.register_vault_card_number("4111 1111 1111 1111")
    other = "Order 5555 5555 5555 4444, ref 4111 1111 1111 111, total 4111.11"
    assert redact.redact_registered_vault_values(other) == other


def test_a_value_that_is_not_a_card_number_registers_only_its_exact_bytes():
    redact.register_vault_card_number("4111 1111 1111 1111 ext")
    assert redact.redact_registered_vault_values("4111111111111111") == "4111111111111111"
    assert redact.redact_registered_vault_values("4111 1111 1111 1111 ext") == _REDACTED


def test_a_card_fill_registers_the_number_for_any_page_format(tmp_path):
    """End to end through browser_vault_fill: a card stored with spaces, filled into a page whose input
    strips them, cannot be read back by a later browser call."""
    from agent.vault_backends.local import LocalLoginBackend
    from tools import browser_vault_tool

    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item(kind="payment", label="Test card", origin="https://shop.example", secret={
        "card_number": "4111 1111 1111 1111", "cardholder_name": "Test User", "exp_month": "07",
        "exp_year": "2029", "cvc": "123", "billing_postal_code": "94107"})

    class _Page:
        href = "https://shop.example/checkout"

        def focus_page(self, origin, accept=None):
            return {"ok": normalize_origin(self.href) == origin, "url": self.href}

        def evaluate_runtime(self, expression):
            if expression.strip() == "window.location.href":
                return {"ok": True, "result": self.href}
            if "expectedOrigin" in expression:
                return {"ok": True, "result": json.dumps({"filled": 1})}
            return {"ok": True, "result": json.dumps([{"autocomplete": "cc-number", "index": 0, "type": "text"}])}

    with patch("agent.vault_store.get_vault_store", return_value=store), \
         patch("agent.vault_backends.backend_for_handle", return_value=LocalLoginBackend()), \
         patch.object(browser_vault_tool, "_ensure_supervisor", return_value=_Page()), \
         patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
        out = json.loads(browser_vault_tool.browser_vault_fill(card.id, task_id="t"))
    assert out["success"] is True
    assert redact.redact_registered_vault_values('input.value = "4111111111111111"') == f'input.value = "{_REDACTED}"'
