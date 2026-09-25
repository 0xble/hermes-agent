from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from agent.vault_backends.onepassword import OnePasswordLoginBackend, _normalize_protected_date
from agent.vault_login_classifier import (
    LoginControl,
    classify_protected_field_control,
    select_protected_field_fills,
)


@pytest.fixture(autouse=True)
def _private_browser_for_protected_fills(monkeypatch):
    from tools import browser_tool
    monkeypatch.setattr(browser_tool, "_active_sessions", {
        task: {"session_key": task, "owner_task_id": task,
               "session_name": f"local-{task}", "features": {"local": True}}
        for task in ("default", "zero")
    })
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: None)
    monkeypatch.setattr("tools.browser_tool_session._shares_bot_desktop_browser", lambda session: False)
    monkeypatch.setattr("tools.browser_camofox.is_camofox_mode", lambda: False)


def _control(*, autocomplete="", index=0, label="", name="", type="text", form_index=0):
    return LoginControl(
        autocomplete=autocomplete,
        form_index=form_index,
        index=index,
        label=label,
        name=name,
        type=type,
    )


def _protected_config(**overrides):
    entry = {
        "label": "Traveler date of birth",
        "reference": "op://vault/item/birthdate",
        "semantic": "bday",
        "origins": ["https://www.example.com"],
        "value_type": "date",
    }
    entry.update(overrides)
    return entry


def test_protected_birth_date_classifier_prefers_standard_tokens_and_bounded_dob_labels():
    # A standard token never overrides the control type: only a control that can
    # show that date part receives it.
    for kind in ("hidden", "checkbox", "radio", "password", "email", "search", "textarea", "range"):
        for token in ("bday", "bday-month", "bday-day", "bday-year"):
            assert classify_protected_field_control(_control(autocomplete=token, type=kind), "bday") is None
    assert classify_protected_field_control(_control(autocomplete="bday", type="text"), "bday") is None
    assert classify_protected_field_control(_control(name="birth_month", type="checkbox"), "bday") is None
    assert classify_protected_field_control(
        _control(autocomplete="bday", type="date"), "bday"
    ).token == "bday"
    assert classify_protected_field_control(
        _control(autocomplete="bday-month", type="select"), "bday"
    ).token == "bday-month"
    assert classify_protected_field_control(
        _control(name="travelerDateOfBirth", label="Date of birth", type="date"), "bday"
    ).token == "bday"
    assert classify_protected_field_control(
        _control(name="departureDate", label="Departure date", type="date"), "bday"
    ) is None
    assert classify_protected_field_control(
        _control(name="birth_month", type="select"), "bday"
    ).token == "bday-month"
    assert classify_protected_field_control(
        _control(name="birth_day", type="select"), "bday"
    ).token == "bday-day"
    assert classify_protected_field_control(
        _control(name="birth_year", type="number"), "bday"
    ).token == "bday-year"
    assert classify_protected_field_control(
        _control(name="birthMonth", type="select"), "bday"
    ).token == "bday-month"
    assert classify_protected_field_control(
        _control(name="birthDay", type="select"), "bday"
    ).token == "bday-day"
    assert classify_protected_field_control(
        _control(name="birthYear", type="number"), "bday"
    ).token == "bday-year"
    assert classify_protected_field_control(
        _control(name="dateOfBirth", label="Date of birth", type="select"), "bday"
    ) is None
    assert classify_protected_field_control(
        _control(label="Day of travel (see birth certificate)", type="select"), "bday"
    ) is None


def test_protected_birth_date_fill_supports_one_date_control_or_one_split_form():
    value = "1990-04-12"
    one = [classify_protected_field_control(_control(autocomplete="bday", type="date"), "bday")]
    assert select_protected_field_fills(one, "bday", value) == [
        {"index": 0, "token": "bday", "value": value}
    ]

    split = [
        classify_protected_field_control(
            _control(autocomplete="bday-month", index=0, type="select", form_index=2), "bday"
        ),
        classify_protected_field_control(
            _control(autocomplete="bday-day", index=1, type="select", form_index=2), "bday"
        ),
        classify_protected_field_control(
            _control(autocomplete="bday-year", index=2, type="number", form_index=2), "bday"
        ),
    ]
    assert select_protected_field_fills(split, "bday", value) == [
        {"index": 0, "token": "bday-month", "value": "04"},
        {"index": 1, "token": "bday-day", "value": "12"},
        {"index": 2, "token": "bday-year", "value": "1990"},
    ]


def test_protected_birth_date_split_never_mixes_forms():
    classified = [
        classify_protected_field_control(_control(autocomplete="bday-month", index=0, form_index=0), "bday"),
        classify_protected_field_control(_control(autocomplete="bday-day", index=1, form_index=1), "bday"),
        classify_protected_field_control(_control(autocomplete="bday-year", index=2, form_index=0), "bday"),
    ]
    assert select_protected_field_fills(classified, "bday", "1990-04-12") == []


def test_protected_date_rejects_ambiguous_eight_digit_numeric_value():
    with pytest.raises(RuntimeError, match="unsupported format"):
        _normalize_protected_date("19900412")
    assert _normalize_protected_date("643766400") == "1990-05-27"
    assert _normalize_protected_date("-315619200") == "1960-01-01"
    with pytest.raises(RuntimeError, match="unsupported format"):
        _normalize_protected_date("-19900412")


def test_onepassword_configured_protected_field_is_metadata_only_and_origin_bound():
    backend = OnePasswordLoginBackend({
        "protected_fields": [_protected_config(
            origins=["https://www.aa.com/reservations/findReservationAccess"]
        )]
    })
    items = backend.list_protected_fields()
    assert len(items) == 1
    meta = items[0]
    assert meta.kind == "protected_field"
    assert meta.field_token == "bday"
    assert meta.origin == "https://www.aa.com"
    assert meta.allowed_origins == ("https://www.aa.com",)
    assert "op://" not in json.dumps(meta.to_dict())

    with patch.object(backend, "_run", return_value="643766400\n") as run:
        assert backend.resolve_secret(meta.id) == {"value": "1990-05-27"}
    assert run.call_args.args == ("read", "--", "op://vault/item/birthdate")


def test_protected_field_handles_are_scoped_to_their_own_account():
    """An additional account's field must route back to that account (and its browser
    binding), never to the primary one, even when both configure the same reference."""
    config = {"protected_fields": [_protected_config()]}
    primary = OnePasswordLoginBackend(config)
    other = OnePasswordLoginBackend(dict(config, browser_account="work"), alias="lpg")
    [primary_meta] = primary.list_protected_fields()
    [other_meta] = other.list_protected_fields()
    assert other_meta.id.startswith("op@lpg:field:") and primary_meta.id.startswith("op:field:")
    assert other_meta.id != primary_meta.id
    assert other.owns(other_meta.id) and not primary.owns(other_meta.id)
    assert primary.owns(primary_meta.id) and not other.owns(primary_meta.id)
    assert other.get_meta(other_meta.id) == other_meta and primary.get_meta(other_meta.id) is None
    with patch.object(other, "_run", return_value="1990-04-12\n") as run:
        assert other.resolve_secret(other_meta.id) == {"value": "1990-04-12"}
    assert run.call_args.args == ("read", "--", "op://vault/item/birthdate")
    with patch.object(primary, "_run") as primary_run, pytest.raises(Exception):
        primary.resolve_secret(other_meta.id)
    primary_run.assert_not_called()


def test_invalid_protected_field_config_is_not_advertised():
    invalid = [
        _protected_config(origins=["http://www.example.com"]),
        _protected_config(semantic="passport-number"),
        _protected_config(reference="op://vault"),
    ]
    assert OnePasswordLoginBackend({"protected_fields": invalid}).list_protected_fields() == []


def test_malformed_protected_field_reference_does_not_break_valid_or_login_listing():
    login_listing = json.dumps([{
        "id": "login-id",
        "category": "LOGIN",
        "title": "Example login",
        "urls": [{"href": "https://www.example.com/login"}],
        "additional_information": "traveler@example.com",
    }])
    backend = OnePasswordLoginBackend({
        "protected_fields": [
            _protected_config(reference="op://[bad/item/x"),
            _protected_config(),
        ]
    })
    with patch.object(backend, "is_unlocked", return_value=True), \
         patch.object(backend, "_run", return_value=login_listing):
        items = backend.list_items()

    assert [item.kind for item in items] == ["login", "protected_field"]
    assert items[0].id == "op:login-id"


def test_connect_only_backend_does_not_advertise_unresolvable_protected_fields():
    secrets = {
        "OP_CONNECT_HOST": "https://connect.example.com",
        "OP_CONNECT_TOKEN": "synthetic-token",
    }
    with patch("agent.secret_scope.get_secret", side_effect=lambda key, default="": secrets.get(key, default)):
        backend = OnePasswordLoginBackend({"protected_fields": [_protected_config()]})
        assert backend.is_unlocked()
        assert backend.list_protected_fields() == []
        assert backend.get_meta("op:field:not-advertised") is None


def test_browser_vault_fill_injects_configured_protected_field_without_disclosing_value():
    from agent.vault_store import VaultItemMeta
    from tools import browser_vault_tool

    secret = "1990-04-12"
    meta = VaultItemMeta(
        id="op:field:fixture",
        kind="protected_field",
        label="Traveler date of birth",
        origin="https://www.aa.com",
        created_at="",
        allowed_origins=("https://www.aa.com",),
        field_token="bday",
    )

    class Backend:
        name = "onepassword"
        display_name = "1Password"
        needs_unlock = False
        binds_cards_to_page = False

        def is_unlocked(self):
            return True

        def get_meta(self, handle):
            return meta if handle == meta.id else None

        def resolve_secret(self, handle):
            return {"value": secret}

    controls = [{
        "autocomplete": "bday",
        "formIndex": 0,
        "index": 0,
        "label": "Date of birth",
        "name": "dateOfBirth",
        "type": "date",
    }]
    expressions = []

    def fake_eval(_task_id, expression):
        return {"success": True, "result": json.dumps(controls)}

    def fake_secret_eval(_task_id, expression):
        expressions.append(expression)
        return {"success": True, "result": json.dumps({"filled": 1})}

    with patch("agent.vault_backends.backend_for_handle", return_value=Backend()), \
         patch.object(browser_vault_tool, "_current_page_origin", return_value="https://www.aa.com"), \
         patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
         patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_secret_eval):
        raw = browser_vault_tool.browser_vault_fill(meta.id)

    out = json.loads(raw)
    assert out == {
        "success": True,
        "filled_fields": 1,
        "backend": "onepassword",
        "kind": "protected_field",
        "origin": "https://www.aa.com",
        "fields": ["bday"],
    }
    assert secret not in raw
    assert secret in expressions[0]


def test_protected_field_origin_mismatch_never_resolves_secret():
    from agent.vault_store import VaultItemMeta
    from tools import browser_vault_tool

    meta = VaultItemMeta(
        id="op:field:fixture",
        kind="protected_field",
        label="Traveler date of birth",
        origin="https://allowed.example",
        created_at="",
        allowed_origins=("https://allowed.example",),
        field_token="bday",
    )
    backend = Mock(
        name="onepassword", display_name="1Password", needs_unlock=False,
        binds_cards_to_page=False,
    )
    backend.get_meta.return_value = meta
    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
         patch.object(browser_vault_tool, "_current_page_origin", return_value="https://other.example"):
        out = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
    assert out["error_type"] == "origin_mismatch"
    backend.resolve_secret.assert_not_called()


def test_split_protected_date_components_are_redacted_before_page_write():
    from agent.redact import clear_vault_redaction_values, redact_registered_vault_values
    from agent.vault_store import VaultItemMeta
    from tools import browser_vault_tool

    meta = VaultItemMeta(
        id="op:field:split-fixture", kind="protected_field", label="DOB",
        origin="https://allowed.example", created_at="",
        allowed_origins=("https://allowed.example",), field_token="bday",
    )
    backend = Mock(display_name="1Password", needs_unlock=False,
                   binds_cards_to_page=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_secret.return_value = {"value": "1990-04-12"}
    controls = [
        {"autocomplete": f"bday-{part}", "formIndex": 0, "index": i,
         "label": part, "name": f"birth_{part}", "type": "select"}
        for i, part in enumerate(("month", "day", "year"))
    ]
    def eval_page(_task, expression):
        return {"success": True, "result": json.dumps(controls)}

    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value=meta.origin), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=eval_page), \
             patch.object(browser_vault_tool, "_eval_js_secret", return_value={"success": True, "result": '{"filled":3}'}):
            result = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
        assert result["filled_fields"] == 3
        text = redact_registered_vault_values("month=4 day=12 year=1990")
        assert "1990" not in text and "12" not in text and "4" not in text
        assert redact_registered_vault_values("4 bookings") == "«redacted-vault-secret» bookings"
    finally:
        clear_vault_redaction_values()


def test_partial_split_date_fill_reports_failure_not_success():
    """If one part (e.g. an ambiguous month menu) is refused, the date on the page is
    incomplete, so the fill must not report success."""
    from agent.redact import clear_vault_redaction_values
    from agent.vault_store import VaultItemMeta
    from tools import browser_vault_tool

    meta = VaultItemMeta(
        id="op:field:split-partial", kind="protected_field", label="DOB",
        origin="https://allowed.example", created_at="",
        allowed_origins=("https://allowed.example",), field_token="bday",
    )
    backend = Mock(display_name="1Password", needs_unlock=False, binds_cards_to_page=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_secret.return_value = {"value": "1990-04-12"}
    controls = [
        {"autocomplete": f"bday-{part}", "formIndex": 0, "index": i,
         "label": part, "name": f"birth_{part}", "type": "select"}
        for i, part in enumerate(("month", "day", "year"))
    ]
    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value=meta.origin), \
             patch.object(browser_vault_tool, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}), \
             patch.object(browser_vault_tool, "_eval_js_secret", return_value={"success": True, "result": '{"filled":2}'}):
            result = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
        assert result["success"] is False and result["error_type"] == "partial_fill"
        assert "1990" not in json.dumps(result)
    finally:
        clear_vault_redaction_values()


def test_zero_based_and_opaque_selected_option_values_are_masked():
    """The option actually selected can carry a value unlike the date (April as "3" or
    "x3"); a later el.value read must still be masked."""
    from agent.browser_output_egress import scrub_browser_result
    from agent.redact import clear_vault_date_components, clear_vault_redaction_values
    from agent.vault_store import VaultItemMeta
    from tools import browser_vault_tool

    meta = VaultItemMeta(
        id="op:field:split-zero", kind="protected_field", label="DOB",
        origin="https://allowed.example", created_at="",
        allowed_origins=("https://allowed.example",), field_token="bday",
    )
    backend = Mock(display_name="1Password", needs_unlock=False, binds_cards_to_page=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_secret.return_value = {"value": "1990-04-12"}
    controls = [
        {"autocomplete": f"bday-{part}", "formIndex": 0, "index": i,
         "label": part, "name": f"birth_{part}", "type": "select"}
        for i, part in enumerate(("month", "day", "year"))
    ]
    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value=meta.origin), \
             patch.object(browser_vault_tool, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}), \
             patch.object(browser_vault_tool, "_eval_js_secret", return_value={
                 "success": True, "result": json.dumps({"filled": 3, "selectedOptions": [
                     {"token": "bday-month", "value": "3", "label": "April"},
                     {"token": "bday-day", "value": "d-12x", "label": "12"},
                     {"token": "bday-year", "value": "1990", "label": "1990"},
                 ]})}):
            assert json.loads(browser_vault_tool.browser_vault_fill(meta.id, task_id="zero"))["success"]
        out = json.loads(scrub_browser_result(
            "browser_console", json.dumps({"month": "3", "n": 3, "day": "d-12x"}), "zero"))
        assert "3" not in json.dumps(out) and "d-12x" not in json.dumps(out)
    finally:
        clear_vault_date_components("zero")
        clear_vault_redaction_values()


def test_split_date_selected_option_label_is_redacted_without_masking_unrelated_digits():
    from agent.redact import clear_vault_redaction_values, redact_registered_vault_values
    MASK = '«redacted-vault-secret»'
    from agent.vault_store import VaultItemMeta
    from tools import browser_vault_tool

    meta = VaultItemMeta(
        id="op:field:split-label", kind="protected_field", label="DOB",
        origin="https://allowed.example", created_at="",
        allowed_origins=("https://allowed.example",), field_token="bday",
    )
    backend = Mock(display_name="1Password", needs_unlock=False, binds_cards_to_page=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_secret.return_value = {"value": "1990-04-12"}
    controls = [
        {"autocomplete": f"bday-{part}", "formIndex": 0, "index": i,
         "label": part, "name": f"birth_{part}", "type": "select"}
        for i, part in enumerate(("month", "day", "year"))
    ]
    try:
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value=meta.origin), \
             patch.object(browser_vault_tool, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}), \
             patch.object(browser_vault_tool, "_eval_js_secret", return_value={
                 "success": True, "result": json.dumps({"filled": 3, "selectedOptions": [
                     {"token": "bday-month", "value": "4", "label": "April"},
                     {"token": "bday-day", "value": "12", "label": "12th"},
                     {"token": "bday-year", "value": "1990", "label": "1990"},
                 ]})}):
            assert json.loads(browser_vault_tool.browser_vault_fill(meta.id))["filled_fields"] == 3
        assert "April" not in redact_registered_vault_values('selected month: April')
        assert "12th" not in redact_registered_vault_values('selected day: 12th')
        assert redact_registered_vault_values('4 bookings, 3 guests; 04 rooms; 12 bags; 1990 report') == f'{MASK} bookings, 3 guests; {MASK} rooms; {MASK} bags; {MASK} report'
        for component, value in (("month", "4"), ("month", "04"), ("day", "12"), ("year", "1990")):
            assert value not in redact_registered_vault_values(f'{component}={value}')
        assert '"4"' not in redact_registered_vault_values('{"result":{"type":"string","value":"4"}}')
        assert redact_registered_vault_values('4') == MASK
        from tools.browser_tool_snapshot import _redact_browser_output
        from tools.browser_cdp_tool import _redact_cdp_output
        from tools.browser_camofox import _fetch_snapshot
        from agent.redact import vault_read_has_protected_field_context
        assert not vault_read_has_protected_field_context("document.querySelector('[name=\\\"birth_month\\\"]').value")
        assert not vault_read_has_protected_field_context("document.querySelectorAll('select')[0].value")
        assert not vault_read_has_protected_field_context("document.querySelectorAll('select')[9].value")
        assert _redact_browser_output(4) == MASK
        assert _redact_cdp_output({'result': {'value': 4}})['result']['value'] == MASK
        assert _redact_cdp_output({'result': {'value': 4}}, field_context=True)['result']['value'] == MASK
        with patch('tools.browser_camofox._snapshot_data', return_value={
            'snapshot': 'birth month: 4\\nother: 4 bookings', 'refsCount': 2
        }):
            snapshot, count = _fetch_snapshot({'tab_id': 'dummy'})
        assert count == 2 and 'April' not in snapshot and 'birth month: 4' not in snapshot and '4 bookings' not in snapshot
        assert 'option "4"' not in redact_registered_vault_values('option "4" [selected]')
        assert 'value="4"' not in redact_registered_vault_values('<option value="4" selected>April</option>')
    finally:
        clear_vault_redaction_values()


def test_protected_birth_date_split_needs_a_shared_form_or_container():
    def part(token, index, container):
        return classify_protected_field_control(LoginControl(
            autocomplete=token, form_index=None, index=index, label="", name="", type="select",
            container_index=container), "bday")

    unrelated = [part("bday-month", 0, None), part("bday-day", 1, None), part("bday-year", 2, None)]
    assert select_protected_field_fills(unrelated, "bday", "1990-04-12") == []
    split_widgets = [part("bday-month", 0, 1), part("bday-day", 1, 2), part("bday-year", 2, 1)]
    assert select_protected_field_fills(split_widgets, "bday", "1990-04-12") == []
    one_widget = [part("bday-month", 0, 3), part("bday-day", 1, 3), part("bday-year", 2, 3)]
    assert [f["token"] for f in select_protected_field_fills(one_widget, "bday", "1990-04-12")] == [
        "bday-month", "bday-day", "bday-year"]
