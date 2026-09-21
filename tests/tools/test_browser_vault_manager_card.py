"""A password manager's card binds to the page the browser is on; the user confirms that exact origin.

Local-vault cards keep their saved-origin binding (no origin → refused). Manager cards have no site of
their own, so the fill resolves the current page origin, puts it in the confirmation prompt, then runs
the same origin-checked fill as a local card. The card values never appear in any returned string.
"""
import json
from unittest.mock import patch

import pytest

from agent.vault_backends.base import LoginBackend
from agent.vault_store import VaultItemMeta, VaultStore

_CARD = {"card_number": "4111111111111111", "cardholder_name": "A User", "exp_month": "07", "exp_year": "2029", "cvc": "9876"}
_CONTROLS = [
    {"autocomplete": "cc-number", "index": 0, "type": "text"},
    {"label": "Expiry (MM/YY)", "index": 1, "type": "text"},
    {"label": "CVC", "index": 2, "type": "text"},
]


@pytest.fixture()
def store(tmp_path):
    return VaultStore(base_dir=tmp_path / "vault")


class _Manager(LoginBackend):
    name = "manager"
    display_name = "Manager"
    prefix = "mg:"
    needs_unlock = True
    binds_cards_to_page = True
    meta = VaultItemMeta(id="mg:card", kind="payment", label="Amex", origin=None, created_at="",
                         identifier_type="card_last4", identifier="1111")

    def is_unlocked(self):
        return True

    def unlock(self, master_password):
        pass

    def list_items(self):
        return [self.meta]

    def get_meta(self, handle):
        return self.meta if handle == self.meta.id else None

    def resolve_password(self, handle):
        raise RuntimeError("a card is never a login")

    def resolve_secret(self, handle):
        return dict(_CARD)


def _run_fill(page_url, decision="accept", focused=None):
    from tools import browser_vault_tool
    prompts, secret_exprs, focus_calls = [], [], []
    page = {"url": page_url}

    def fake_focus(task_id, origin, kind):
        # Mirrors _focus_bound_origin: returns the requested origin (or the focused tab's URL when asked
        # for any tab) once a tab holding a payment form is focused, else None.
        focus_calls.append((origin, kind))
        if not focused:
            return None
        page["url"] = focused  # focusing the checkout tab changes what the page session reports next
        return origin or focused

    def fake_eval(task_id, expression):
        if "location.href" in expression:
            return {"success": True, "result": page["url"]}
        return {"success": True, "result": json.dumps(_CONTROLS)}

    def fake_eval_secret(task_id, expression):
        secret_exprs.append(expression)
        return {"success": True, "result": json.dumps({"filled": 3})}

    def consent(message, description, **kw):
        prompts.append(message)
        return decision

    backend = _Manager()
    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch.object(browser_vault_tool, "_focus_bound_origin", side_effect=fake_focus), \
         patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
         patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret), \
         patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
        raw = browser_vault_tool.browser_vault_fill("mg:card")
    _run_fill.focus_calls = focus_calls
    return raw, prompts, secret_exprs


def test_manager_card_binds_to_current_page_and_confirmation_names_it():
    from agent import redact
    try:
        raw, prompts, secret_exprs = _run_fill("https://shop.test/checkout?step=pay")
        out = json.loads(raw)
        assert out["success"] is True and out["origin"] == "https://shop.test"
        assert out["fields"] == ["cc-csc", "cc-exp", "cc-number"]
        assert prompts == ["Fill payment card 'Amex' on https://shop.test"]
        # The checkout tab is focused before the origin is read (the supervisor's default tab may be blank).
        assert _run_fill.focus_calls[0] == ("", "payment")
        assert len(secret_exprs) == 1 and _CARD["card_number"] in secret_exprs[0] and "07/29" in secret_exprs[0]
        assert '"https://shop.test"' in secret_exprs[0]  # the fill script re-checks the bound origin itself
        assert _CARD["card_number"] not in raw and _CARD["cvc"] not in raw
    finally:
        redact.clear_vault_redaction_values()


def test_manager_card_uses_the_focused_checkout_tab_over_the_default_page():
    from agent import redact
    try:
        # Default page session reports a blank tab; the focused payment-form tab wins.
        raw, prompts, _ = _run_fill("about:blank", focused="https://shop.test/pay")
        assert json.loads(raw)["origin"] == "https://shop.test"
        assert prompts == ["Fill payment card 'Amex' on https://shop.test"]
    finally:
        redact.clear_vault_redaction_values()


def test_manager_card_declined_writes_nothing():
    raw, prompts, secret_exprs = _run_fill("https://shop.test/checkout", decision="decline")
    assert json.loads(raw)["error_type"] == "payment_declined"
    assert prompts == ["Fill payment card 'Amex' on https://shop.test"] and secret_exprs == []


def test_manager_card_without_a_page_is_refused_before_prompting():
    raw, prompts, secret_exprs = _run_fill("about:blank")
    out = json.loads(raw)
    assert out["success"] is False and "origin" in out["error"].lower()
    assert prompts == [] and secret_exprs == []


def test_local_card_without_origin_is_still_refused(store):
    from tools import browser_vault_tool
    meta = store.add_item(kind="payment", label="Visa", secret=_CARD)
    with patch("agent.vault_store.get_vault_store", return_value=store), \
         patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
        out = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
    assert out["error_type"] == "no_origin"
    consent.assert_not_called()


def test_listing_marks_manager_card_available():
    from tools import browser_vault_tool
    with patch("agent.vault_backends.enabled_backends", return_value=[_Manager()]):
        out = json.loads(browser_vault_tool.browser_vault_list())
    (entry,) = [i for i in out["items"] if i["handle"] == "mg:card"]
    assert entry["available"] is True and entry["origin"] is None and entry["identifier"] == "1111"
