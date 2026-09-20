"""1Password Credit Card items surface as payment handles and resolve to the PAYMENT_FIELDS shape.

Metadata never carries the PAN or CVV: the listing exposes only the masked number, so the handle
shows last-four digits and no origin. The browser fill binds a manager's card to the page it is on
and asks the user to confirm that exact origin (tests/tools/test_browser_vault_manager_card.py).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope
from agent.vault_backends.onepassword import OnePasswordLoginBackend, _card_secret

_LIST = "item", "list", "--categories", "Login,Credit Card", "--format", "json"
_PAN = "4111111111111111"
_CVV = "9876"


def _card_fields(**overrides):
    fields = {
        "cardholder": {"id": "cardholder", "label": "cardholder name", "type": "STRING", "value": "A User"},
        "ccnum": {"id": "ccnum", "label": "number", "type": "CREDIT_CARD_NUMBER", "value": "4111 1111 1111 1111"},
        "cvv": {"id": "cvv", "label": "verification number", "type": "CONCEALED", "value": _CVV},
        "expiry": {"id": "expiry", "label": "expiry date", "type": "MONTH_YEAR", "value": "202907"},
        "zip": {"id": "l43r6bffr7bvmxmqprd7tihmly", "label": "ZIP", "type": "STRING", "value": "94110"},
        "pin": {"id": "pin", "label": "PIN", "type": "CONCEALED", "value": "0000"},
    }
    fields.update(overrides)
    return [f for f in fields.values() if f is not None]


def _login(item_id="login-a", vault="vault-a"):
    return {"id": item_id, "title": "Site", "category": "LOGIN", "vault": {"id": vault},
            "urls": [{"href": "https://example.com/login"}], "additional_information": "user@example.com"}


def _card(item_id="card-a", vault="vault-a", masked="4111 **** 1111"):
    return {"id": item_id, "title": "Visa", "category": "CREDIT_CARD", "vault": {"id": vault},
            "additional_information": masked}


@pytest.fixture
def backend():
    with patch("agent.secret_scope.get_secret", return_value="dummy-service-token"):
        backend = OnePasswordLoginBackend()
    backend._run = Mock()
    return backend


def test_card_secret_maps_op_fields_onto_payment_shape():
    secret = _card_secret(_card_fields())
    assert secret == {"card_number": _PAN, "cardholder_name": "A User", "cvc": _CVV,
                      "exp_month": "07", "exp_year": "2029", "billing_postal_code": "94110"}
    # Unrelated concealed fields (PIN, security number) never ride along into a page fill.
    assert "0000" not in secret.values()


def test_card_secret_ignores_malformed_expiry_and_blank_values():
    secret = _card_secret(_card_fields(expiry={"id": "expiry", "value": "07/29"}, zip={"id": "x", "label": "ZIP", "value": " "}))
    assert "exp_month" not in secret and "exp_year" not in secret and "billing_postal_code" not in secret
    assert _card_secret("not-a-list") == {}


def test_listing_exposes_cards_as_payment_handles_without_secrets(backend):
    backend._run.return_value = json.dumps([_login(), _card()])
    metas = {m.id: m for m in backend.list_items()}
    assert metas["op:login-a"].kind == "login" and metas["op:login-a"].origin == "https://example.com"
    card = metas["op:card-a"]
    assert (card.kind, card.label, card.origin, card.identifier_type, card.identifier) == \
        ("payment", "Visa", None, "card_last4", "1111")
    assert backend._run.call_args.args == _LIST
    assert backend.get_meta("op:card-a") == card
    assert _PAN not in json.dumps([m.to_dict() for m in metas.values()])


def test_card_without_masked_number_still_lists(backend):
    backend._run.return_value = json.dumps([_card(masked="")])
    (card,) = backend.list_items()
    assert card.kind == "payment" and card.identifier is None and card.identifier_type is None


def test_resolve_secret_reads_card_in_its_vault_and_returns_payment_shape(backend):
    backend._run.side_effect = [json.dumps([_login(), _card(vault="vault-b")]),
                                json.dumps({"id": "card-a", "category": "CREDIT_CARD", "fields": _card_fields()})]
    secret = backend.resolve_secret("op:card-a")
    assert secret["card_number"] == _PAN and secret["cvc"] == _CVV and (secret["exp_month"], secret["exp_year"]) == ("07", "2029")
    assert backend._run.call_args_list[1].args == (
        "item", "get", "card-a", "--vault", "vault-b", "--format", "json", "--reveal")


def test_resolve_secret_on_a_login_keeps_password_only_shape(backend):
    backend._run.side_effect = [json.dumps([_login(), _card()]), "pw\n"]
    assert backend.resolve_secret("op:login-a") == {"password": "pw"}
    assert backend._run.call_count == 2  # one listing serves both the category branch and the vault selector
    assert backend._run.call_args.args == ("item", "get", "login-a", "--vault", "vault-a", "--fields", "label=password", "--reveal")


def test_card_handle_never_resolves_as_a_login_password(backend):
    # A card handle passed to the login path must fail closed rather than reveal any field.
    backend._run.return_value = json.dumps([_login(), _card()])
    with pytest.raises(RuntimeError):
        backend.resolve_password("op:card-a")
    assert backend.resolve_otp("op:card-a") is None
    assert all(c.args[:2] != ("item", "get") for c in backend._run.call_args_list)


@pytest.mark.parametrize("missing", ["ccnum", "cvv", "expiry"])
def test_incomplete_card_fails_closed(backend, missing):
    backend._run.side_effect = [json.dumps([_card()]),
                                json.dumps({"id": "card-a", "fields": _card_fields(**{missing: None})})]
    with pytest.raises(RuntimeError, match="missing"):
        backend.resolve_secret("op:card-a")


def _connect_server(item):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            listing = {k: v for k, v in item.items() if k != "fields"}
            routes = {"/v1/vaults": [{"id": item["vault"]["id"]}],
                      f"/v1/vaults/{item['vault']['id']}/items": [listing],
                      f"/v1/vaults/{item['vault']['id']}/items/{item['id']}": item}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(routes.get(self.path, {})).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_connect_lists_and_resolves_cards(monkeypatch):
    vault, item_id = "v" * 26, "c" * 26
    server = _connect_server({"id": item_id, "vault": {"id": vault}, "category": "CREDIT_CARD", "title": "Amex",
                              "fields": _card_fields()})
    scope = set_secret_scope({"OP_CONNECT_HOST": f"http://127.0.0.1:{server.server_port}", "OP_CONNECT_TOKEN": "t"})
    try:
        backend = OnePasswordLoginBackend()
        (card,) = backend.list_items()
        # The Connect listing omits fields, so last-four is unknown there; the item read supplies it.
        assert card.kind == "payment" and card.origin is None and card.id == f"op:connect:{vault}:{item_id}"
        assert backend.get_meta(card.id).identifier == "1111"
        assert backend.resolve_secret(card.id)["card_number"] == _PAN
        with pytest.raises(RuntimeError):
            backend.resolve_password(card.id)  # a card is never a login
    finally:
        reset_secret_scope(scope)
        server.shutdown()
