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


def _run_fill(page_url, decision="accept", focused=None, controls=_CONTROLS, after_consent=None):
    from tools import browser_vault_tool
    prompts, secret_exprs, focus_calls, order, nonces = [], [], [], [], []
    page = {"url": page_url, "controls": controls}

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
        order.append("inspect")
        nonces.append(expression.split("const nonce = ", 1)[1].split(";", 1)[0])
        return {"success": True, "result": json.dumps(page["controls"])}

    def fake_eval_secret(task_id, expression):
        secret_exprs.append(expression)
        return {"success": True, "result": json.dumps({"filled": 3})}

    def consent(message, description, **kw):
        order.append("prompt")
        prompts.append(message)
        if after_consent is not None:
            page["controls"] = after_consent  # the page changed while the prompt waited
        return decision

    backend = _Manager()
    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch.object(browser_vault_tool, "_focus_bound_origin", side_effect=fake_focus), \
         patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
         patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret), \
         patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
        raw = browser_vault_tool.browser_vault_fill("mg:card")
    _run_fill.focus_calls = focus_calls
    _run_fill.order = order
    _run_fill.nonces = nonces
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


def test_card_fill_scopes_low_entropy_values_but_not_pan_or_cvc(monkeypatch):
    from agent import redact
    from agent.browser_output_egress import scrub_browser_result
    from tools import browser_vault_tool
    monkeypatch.setitem(_CARD, "billing_postal_code", "94110")
    monkeypatch.setitem(_CARD, "exp_month", "7")
    try:
        with patch.object(browser_vault_tool, "_browser_key", return_value="sidecar"):
            assert json.loads(_run_fill("https://shop.test/checkout")[0])["success"]
        log = "2029-09-28 12:39:07,821 INFO user=A User line 708 postal=94110"
        assert redact.redact_registered_vault_values(log) == log
        assert redact.redact_registered_vault_values(log, tab="elsewhere", origin="https://shop.test") == log
        assert redact.redact_registered_vault_values(log, tab="sidecar", origin="https://other.test") == log
        assert redact.redact_sensitive_text(log, force=True) == log
        assert not redact.has_vault_date_components("sidecar")  # card metadata must not block pixels
        page = 'name=A User zip=94110 month=7 padded=07 year=2029 short=29 n=1708 x94110y'
        with patch.object(browser_vault_tool, "_current_page_origin", return_value="https://other.test"):
            assert json.loads(scrub_browser_result("browser_snapshot", json.dumps({"snapshot": page}), "sidecar"))["snapshot"] == page
        with patch.object(browser_vault_tool, "_current_page_origin", return_value="https://shop.test"):
            output = json.loads(scrub_browser_result("browser_snapshot", json.dumps({"snapshot": page}), "sidecar"))["snapshot"]
        assert "name=A User" not in output and "zip=94110" not in output
        assert "month=7" not in output and "padded=07" not in output
        assert "year=2029" not in output and "short=29" not in output
        assert "n=1708 x94110y" in output
        for value in (_CARD["card_number"], _CARD["cvc"]):
            assert redact.redact_registered_vault_values(f"secret={value}", tab="elsewhere") == "secret=«redacted-vault-secret»"
    finally:
        redact.clear_vault_redaction_values()


def test_card_registrations_cannot_evict_birthday_quarantine():
    from agent import redact
    tab, origin = "mixed-vault-tab", "https://checkout.test"
    redact.mark_vault_protected_tab(tab, origin)
    redact.register_vault_date_component("bday-year", "1984", tab=tab, origin=origin)
    try:
        for index in range(64):
            redact.register_vault_card_component(
                "cardholder_name", f"Cardholder {index}", tab=tab, origin=origin,
            )
        assert redact.has_vault_date_components(tab)
        assert redact.has_any_vault_date_components()
        assert redact.redact_registered_vault_values("born 1984", tab=tab, origin="https://other.test") == (
            "born «redacted-vault-secret»"
        )
    finally:
        redact.clear_vault_date_components(tab)


def test_card_registration_cannot_mask_empty_protected_marker():
    from agent import redact
    tab, origin = "marked-vault-tab", "https://checkout.test"
    redact.mark_vault_protected_tab(tab, origin)
    try:
        redact.register_vault_card_component("exp_year", "2029", tab=tab, origin=origin)
        assert redact.has_vault_date_components(tab)
        assert redact.has_any_vault_date_components()
    finally:
        redact.clear_vault_date_components(tab)


def test_card_only_scope_redacts_only_on_filled_origin_without_birthday_quarantine():
    from agent import redact
    tab, origin = "card-only-vault-tab", "https://checkout.test"
    redact.register_vault_card_component("cardholder_name", "Synthetic Cardholder", tab=tab, origin=origin)
    try:
        assert redact.has_vault_scoped_components(tab)
        assert not redact.has_vault_date_components(tab)
        assert not redact.has_any_vault_date_components()
        assert redact.redact_registered_vault_values("Synthetic Cardholder", tab=tab, origin=origin) == (
            "«redacted-vault-secret»"
        )
        assert redact.redact_registered_vault_values("Synthetic Cardholder", tab=tab, origin="https://other.test") == (
            "Synthetic Cardholder"
        )
    finally:
        redact.clear_vault_date_components(tab)


def test_manager_card_uses_the_focused_checkout_tab_over_the_default_page():
    from agent import redact
    try:
        # Default page session reports a blank tab; the focused payment-form tab wins.
        raw, prompts, _ = _run_fill("about:blank", focused="https://shop.test/pay")
        assert json.loads(raw)["origin"] == "https://shop.test"
        assert prompts == ["Fill payment card 'Amex' on https://shop.test"]
    finally:
        redact.clear_vault_redaction_values()


@pytest.mark.parametrize("decision, expected", [("decline", "payment_declined"), ("cancel", "payment_prompt_unanswered")])
def test_card_prompt_refusal_blocks_retries_on_same_origin_but_not_other_origin(decision, expected):
    from tools import browser_vault_tool as vault
    page = {"url": "https://shop.test/pay"}
    prompts = []
    writes = []

    def evaluate(task, expression):
        if "location.href" in expression:
            return {"success": True, "result": page["url"]}
        return {"success": True, "result": json.dumps(_CONTROLS)}

    def consent(*args, **kwargs):
        prompts.append(args[0])
        return decision

    def write(task, expression):
        writes.append(expression)
        return {"success": True, "result": json.dumps({"filled": 1})}

    with patch("agent.vault_backends.backend_for_handle", return_value=_Manager()), \
         patch.object(vault, "_eval_js", side_effect=evaluate), \
         patch.object(vault, "_eval_js_secret", side_effect=write), \
         patch.object(vault, "_focus_bound_origin", return_value=None), \
         patch("tools.approval_context.get_current_session_key", return_value="retry-session"), \
         patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
        first = json.loads(vault.browser_vault_fill("mg:card", task_id="task-a"))
        second = json.loads(vault.browser_vault_fill("mg:card", task_id="task-a"))
        page["url"] = "https://else.test/pay"
        third = json.loads(vault.browser_vault_fill("mg:card", task_id="task-a"))
    assert first["error_type"] == expected
    assert second["error_type"] == "payment_retry_refused"
    assert "user" in second["error"].lower()
    assert third["error_type"] == expected
    assert len(prompts) == 2 and not writes


def test_card_retry_guard_expires_and_is_session_scoped():
    from tools import browser_vault_tool as vault
    with patch("tools.approval_context.get_current_session_key", return_value="session-one"), \
         patch.object(vault.time, "monotonic", return_value=1000):
        key = vault._payment_retry_key("task", "https://shop.test")
        vault._payment_retry_blocked(key, refuse=True)
        assert vault._payment_retry_blocked(key)
    with patch("tools.approval_context.get_current_session_key", return_value="session-two"):
        assert not vault._payment_retry_blocked(vault._payment_retry_key("task", "https://shop.test"))
    with patch.object(vault.time, "monotonic", return_value=1601):
        assert not vault._payment_retry_blocked(key)


def test_manager_card_declined_writes_nothing():
    raw, prompts, secret_exprs = _run_fill("https://shop.test/checkout", decision="decline")
    assert json.loads(raw)["error_type"] == "payment_declined"
    assert prompts == ["Fill payment card 'Amex' on https://shop.test"] and secret_exprs == []
    # The user is asked only after the page was inspected and found to hold card targets.
    assert _run_fill.order == ["inspect", "prompt"]


@pytest.mark.parametrize("controls", [
    [],  # card inputs live in a processor's cross-origin frame the inspection never enters
    [{"autocomplete": "postal-code", "index": 0, "type": "text"},
     {"autocomplete": "cc-name", "index": 1, "type": "text"}],  # billing inputs only, card number framed
], ids=["no-controls", "no-card-number"])
def test_card_fill_that_cannot_pay_never_prompts(controls):
    raw, prompts, secret_exprs = _run_fill("https://shop.test/checkout", controls=controls)
    out = json.loads(raw)
    assert out["success"] is False and out["error_type"] == "no_payment_fields"
    assert prompts == [] and secret_exprs == []


def test_slow_card_confirmation_without_refresh_support_preserves_base_fill(monkeypatch):
    from agent import redact
    from tools import browser_vault_tool as vault
    try:
        clock = [1000.0]
        monkeypatch.setattr(vault.time, "monotonic", lambda: clock[0])

        def accept_after_wait(*_):
            clock[0] += 31.0
            return "accept"

        monkeypatch.setattr(vault, "_confirm_payment_fill", accept_after_wait)
        raw, _prompts, secret_exprs = _run_fill("https://shop.test/checkout")
        out = json.loads(raw)
        assert out["success"] is True
        assert len(secret_exprs) == 1
    finally:
        redact.clear_vault_redaction_values()


def test_card_fill_writes_only_targets_stamped_after_consent():
    from agent import redact
    try:
        raw, prompts, secret_exprs = _run_fill("https://shop.test/checkout")
        assert json.loads(raw)["success"] is True and len(prompts) == 1
        assert _run_fill.order == ["inspect", "prompt", "inspect"]
        pre, post = _run_fill.nonces
        assert pre != post and post in secret_exprs[0] and pre not in secret_exprs[0]
    finally:
        redact.clear_vault_redaction_values()


def test_card_fields_gone_during_the_prompt_write_nothing():
    raw, prompts, secret_exprs = _run_fill("https://shop.test/checkout", after_consent=[])
    assert json.loads(raw)["error_type"] == "no_payment_fields"
    assert len(prompts) == 1 and secret_exprs == []


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


def test_listing_filters_kind_and_exact_origin_without_losing_unbound_cards():
    from tools import browser_vault_tool as vault
    login = VaultItemMeta(id="mg:login", kind="login", label="Shop", origin="https://shop.test",
                          created_at="", identifier="user@shop.test",
                          allowed_origins=("https://shop.test", "https://other.test"))
    foreign = VaultItemMeta(id="mg:foreign", kind="login", label="Else", origin="https://else.test", created_at="")
    card = _Manager.meta
    backend = _Manager()
    with patch.object(backend, "list_items", return_value=[login, foreign, card]), \
         patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        all_items = json.loads(vault._handle_vault_list({}))
        shop = json.loads(vault._handle_vault_list({"origin": "https://SHOP.test:443/pay"}))
        logins = json.loads(vault._handle_vault_list({"origin": "https://other.test", "kind": "login"}))
        cards = json.loads(vault._handle_vault_list({"kind": "payment"}))
        missing = json.loads(vault._handle_vault_list({"origin": "https://missing.test", "kind": "login"}))
        invalid = json.loads(vault._handle_vault_list({"origin": "not-an-origin"}))
    assert missing["items"] == [] and "hint" in missing
    assert invalid["error_type"] == "invalid_origin"
    assert [item["handle"] for item in all_items["items"]] == ["mg:login", "mg:foreign", "mg:card"]
    assert [item["handle"] for item in shop["items"]] == ["mg:login", "mg:card"]
    assert [item["handle"] for item in logins["items"]] == ["mg:login"]
    assert [item["handle"] for item in cards["items"]] == ["mg:card"]
    assert vault.BROWSER_VAULT_LIST_SCHEMA["parameters"]["properties"]["kind"]["enum"] == [
        "login", "payment", "address", "protected_field"]


def test_listing_marks_manager_card_available():
    from tools import browser_vault_tool
    with patch("agent.vault_backends.enabled_backends", return_value=[_Manager()]):
        out = json.loads(browser_vault_tool.browser_vault_list())
    (entry,) = [i for i in out["items"] if i["handle"] == "mg:card"]
    assert entry["available"] is True and entry["origin"] is None and entry["identifier"] == "1111"
