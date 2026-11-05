"""Real-DOM regression: login and code fills tick only a safe "Remember me"-style box."""

import json
from unittest.mock import patch

import pytest

from agent.vault_login_classifier import (
    LoginControl, build_fill_js, build_inspection_js, classify_login_control, select_password_fill,
)

_LOGIN = """<!doctype html><body>
<form id="login">
  <input type="email" name="email" autocomplete="username">
  <input type="password" name="password" autocomplete="current-password">
  {inside}
  <button type="submit">Sign in</button>
</form>
{outside}
<script>
  window.clicks = [];
  document.addEventListener("change", (e) => { if (e.target.type === "checkbox") window.clicks.push(e.target.id); });
</script>
</body>"""


def _box(box_id, label, attrs=""):
    return f'<label><input type="checkbox" id="{box_id}" {attrs}> {label}</label>'


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api", reason="real-browser fixture needs Playwright")
    try:
        with playwright.sync_playwright() as p, p.chromium.launch(headless=True) as instance:
            yield instance
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium not installed in this environment")
        raise


@pytest.fixture
def page(browser):
    page = browser.new_page()
    # A real http(s) origin: file:// pages report the opaque origin "null".
    page.route("https://login.example/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=page.html))
    yield page
    page.close()


def _open(page, inside="", outside=""):
    page.html = _LOGIN.replace("{inside}", inside).replace("{outside}", outside)
    page.goto("https://login.example/signin")


def _fill(page, remember=True, origin="https://login.example"):
    nonce = "fixture-nonce"
    controls = [LoginControl.from_dict(raw) for raw in json.loads(page.evaluate(build_inspection_js(nonce)))]
    classified = [c for c in (classify_login_control(control) for control in controls) if c]
    fills = select_password_fill(classified, "pw-value")
    return json.loads(page.evaluate(build_fill_js(fills, expected_origin=origin, nonce=nonce, remember=remember)))


def _checked(page, box_id):
    return page.locator(f"#{box_id}").is_checked()


@pytest.mark.parametrize("label", [
    "Remember me", "Keep me signed in", "Stay signed in", "Keep me logged in", "Trust this device",
    "Trust this browser", "Don't ask again on this device", "Remember this computer for 30 days",
])
def test_ticks_remember_style_box_in_the_login_form(page, label):
    _open(page, inside=_box("rm", label))
    result = _fill(page)
    assert result["filled"] == 1 and result["remembered"] is True
    assert _checked(page, "rm")
    assert page.evaluate("window.clicks") == ["rm"]  # a real click: site scripts see the change


def test_name_only_box_and_styled_hidden_native_input(page):
    styled = ('<input type="checkbox" id="rm" name="rememberMe" style="display:none">'
              '<label for="rm">Keep me signed in</label>')
    _open(page, inside=styled)
    assert _fill(page)["remembered"] is True
    assert _checked(page, "rm")


def test_wrapping_label_reads_sibling_span_caption(page):
    styled = ('<label><input type="checkbox" id="opaque-rm">'
              '<span aria-hidden="true"></span><span>Remember me for 30 days</span></label>')
    _open(page, inside=styled)
    result = _fill(page)
    assert result["remembered"] is True
    assert _checked(page, "opaque-rm")


def test_already_ticked_box_stays_ticked(page):
    _open(page, inside=_box("rm", "Remember me", "checked"))
    result = _fill(page)
    assert result["remembered"] is True and _checked(page, "rm")
    assert page.evaluate("window.clicks") == []  # never toggled off


@pytest.mark.parametrize("attrs", ["disabled", 'aria-disabled="true"'])
def test_disabled_box_is_left_alone(page, attrs):
    _open(page, inside=_box("rm", "Remember me", attrs))
    result = _fill(page)
    assert result["filled"] == 1 and result["remembered"] is False


@pytest.mark.parametrize("wrapper", ['<fieldset disabled>{box}</fieldset>',
                                     '<div aria-disabled="true">{box}</div>'])
def test_box_in_disabled_container_is_left_alone(page, wrapper):
    _open(page, inside=wrapper.format(box=_box("rm", "Remember me")))
    assert _fill(page)["remembered"] is False
    assert not _checked(page, "rm")


def test_only_one_safe_box_is_ticked(page):
    _open(page, inside=_box("first", "Remember me") + _box("second", "Trust this device"))
    assert _fill(page)["remembered"] is True
    assert _checked(page, "first") and not _checked(page, "second")
    assert page.evaluate("window.clicks") == ["first"]


def test_hidden_box_is_left_alone(page):
    _open(page, inside=f'<div style="display:none">{_box("rm", "Remember me")}</div>')
    result = _fill(page)
    assert result["remembered"] is False and not _checked(page, "rm")


@pytest.mark.parametrize("label", [
    "Email me marketing offers", "Subscribe to our newsletter", "I agree to the Terms and Conditions",
    "I consent to the privacy policy", "This is a public or shared computer", "Don't remember me",
    "Remember me and send me product updates", "Public computer", "Shared device",
])
def test_decoys_are_never_ticked(page, label):
    # A remember-like name must never outweigh a marketing/consent/shared-computer caption.
    _open(page, inside=_box("remember", label))
    result = _fill(page)
    assert result["filled"] == 1 and result["remembered"] is False
    assert not _checked(page, "remember")


def test_decoy_beside_remember_box_only_remember_is_ticked(page):
    _open(page, inside=_box("news", "Send me the newsletter") + _box("rm", "Remember me"))
    assert _fill(page)["remembered"] is True
    assert _checked(page, "rm") and not _checked(page, "news")


def test_box_outside_the_login_form_is_left_alone(page):
    _open(page, outside=f'<form id="other">{_box("rm", "Remember me")}</form>')
    result = _fill(page)
    assert result["remembered"] is False and not _checked(page, "rm")


def test_form_associated_box_outside_the_container_is_left_alone(page):
    _open(page, outside=_box("rm", "Remember me", 'form="login"'))
    assert _fill(page)["remembered"] is False
    assert not _checked(page, "rm")


def test_formless_login_uses_only_its_tight_container(page):
    page.html = ('<!doctype html><body><div id="far">' + _box("far-rm", "Remember me") + '</div>'
                 '<section><div id="login"><input type="email" autocomplete="username">'
                 '<input type="password" autocomplete="current-password">' + _box("rm", "Stay signed in") +
                 '</div></section></body>')
    page.goto("https://login.example/signin")
    assert _fill(page)["remembered"] is True
    assert _checked(page, "rm") and not _checked(page, "far-rm")


def test_origin_change_writes_and_ticks_nothing(page):
    _open(page, inside=_box("rm", "Remember me"))
    result = _fill(page, origin="https://other.example")
    assert result == {"refused": "origin_changed", "found": "https://login.example"}
    assert not _checked(page, "rm")
    assert page.locator("input[type=password]").input_value() == ""


@pytest.mark.parametrize("checked", [False, True])
def test_remember_off_leaves_box_alone(page, checked):
    _open(page, inside=_box("rm", "Remember me", "checked" if checked else ""))
    result = _fill(page, remember=False)
    assert result["filled"] == 1 and result["remembered"] is False
    assert _checked(page, "rm") is checked and page.evaluate("window.clicks") == []


@pytest.mark.parametrize("split", [False, True])
def test_remember_checked_before_otp_auto_submit(page, split):
    from agent.vault_login_classifier import build_otp_fills, classify_otp_controls

    inputs = ('<input autocomplete="one-time-code" maxlength="1">' * 6 if split else
              '<input autocomplete="one-time-code">')
    page.html = ('<!doctype html><body><form id="mfa">' + inputs + _box("trust", "Trust this device") +
                 '</form><script>window.submissions = [];'
                 'mfa.addEventListener("submit", e => {e.preventDefault(); window.submissions.push(trust.checked);});'
                 'mfa.addEventListener("input", e => {if (e.target.type !== "checkbox" && '
                 '[...mfa.querySelectorAll("input:not([type=checkbox])")].every(n => n.value)) mfa.requestSubmit();});'
                 '</script></body>')
    page.goto("https://login.example/mfa")
    nonce = "otp-timing"
    controls = [LoginControl.from_dict(raw) for raw in json.loads(page.evaluate(build_inspection_js(nonce)))]
    fills = build_otp_fills(classify_otp_controls(controls), "246810")
    outcome = json.loads(page.evaluate(build_fill_js(fills, "https://login.example", nonce, remember=True)))
    assert page.evaluate("window.submissions") == [True]
    assert outcome["remembered"] is True


@pytest.mark.parametrize("layout", ["large_form", "formless"])
def test_fallback_never_checks_another_forms_box(page, layout):
    login = '<div><input type="password" autocomplete="current-password"></div>'
    if layout == "large_form":
        login = '<form id="login">' + login + '<input type="hidden">' * 13 + '</form>'
    page.html = ('<!doctype html><body><main>' + login +
                 '<form id="other">' + _box("foreign", "Remember me") + '</form></main></body>')
    page.goto("https://login.example/signin")
    assert _fill(page)["remembered"] is False
    assert not _checked(page, "foreign")


@pytest.mark.parametrize("mutation", ["disabled = true", "readOnly = true", 'style.display = "none"',
                                       'type = "checkbox"'])
def test_invalidated_otp_target_does_not_tick_remember(page, mutation):
    from agent.vault_login_classifier import build_otp_fills, classify_otp_controls

    page.html = ('<!doctype html><body><form><input id="otp" autocomplete="one-time-code">' +
                 _box("trust", "Trust this device") + '</form></body>')
    page.goto("https://login.example/mfa")
    nonce = "invalid-otp"
    controls = [LoginControl.from_dict(raw) for raw in json.loads(page.evaluate(build_inspection_js(nonce)))]
    fills = build_otp_fills(classify_otp_controls(controls), "246810")
    page.evaluate("document.getElementById('otp')." + mutation)
    outcome = json.loads(page.evaluate(build_fill_js(fills, "https://login.example", nonce, remember=True)))
    assert outcome["remembered"] is False
    assert not _checked(page, "trust")


def test_enter_code_ticks_trust_this_device(page):
    from agent.vault_backends import unlock
    from tools import browser_vault_tool

    page.html = ('<!doctype html><body><form><label for="otp">Verification code</label>'
                 '<input id="otp" autocomplete="one-time-code" inputmode="numeric">'
                 + _box("trust", "Trust this device") + _box("mk", "Email me security tips and offers") +
                 '<button>Verify</button></form></body>')
    page.goto("https://login.example/mfa")
    def evaluate(_task, expression):
        return {"success": True, "result": page.evaluate(expression)}
    unlock.set_code_prompt_callback(lambda site, hint: "246810")
    try:
        with patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value="https://login.example"), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=evaluate), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=evaluate), \
             patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
            outcome = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="fixture"))
    finally:
        unlock.set_code_prompt_callback(None)
    assert outcome["success"] and outcome["remembered"] is True
    assert _checked(page, "trust") and not _checked(page, "mk")
    assert "246810" not in json.dumps(outcome)


@pytest.mark.parametrize("action", ["fill", "enter_code"])
@pytest.mark.parametrize("requested, configured", [(False, True), (True, False), (True, True),
                                                  (None, True), (None, False)])
def test_registered_handler_combines_invocation_and_account_opt_out(page, tmp_path, action, requested, configured):
    from agent.vault_backends import unlock
    from agent.vault_store import VaultStore
    from tools import browser_vault_tool
    from tools.registry import registry

    store = VaultStore(base_dir=tmp_path / "fixture-vault")
    meta = store.add_item(kind="login", label="Fixture", origin="https://login.example",
                          secret={"password": "fixture-password", "identifier": "fixture@example.com",
                                  "identifier_type": "email"})
    if action == "fill":
        _open(page, inside=_box("trust", "Remember me"))
        page.evaluate('window.seen = []; document.querySelector("input[type=password]")'
                      '.addEventListener("input", () => window.seen.push(trust.checked))')
    else:
        page.html = ('<!doctype html><body><form><input id="otp" autocomplete="one-time-code">' +
                     _box("trust", "Trust this device") + '</form><script>window.seen=[];'
                     'otp.addEventListener("input", () => window.seen.push(trust.checked));</script></body>')
        page.goto("https://login.example/mfa")
    def evaluate(_task, expression):
        return {"success": True, "result": page.evaluate(expression)}
    unlock.set_code_prompt_callback(lambda site, hint: "246810")
    try:
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_browser_key", return_value="fixture"), \
             patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value="https://login.example"), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=evaluate), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=evaluate), \
             patch("tools.browser_camofox.is_camofox_mode", return_value=True), \
             patch("tools.browser_camofox.get_session_account", return_value="work"), \
             patch("hermes_cli.config.load_config_readonly",
                   return_value={"vault": {"remember_logins": {"default": True, "work": configured}}}), \
             patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
            args = {"action": action}
            if requested is not None:
                args["remember"] = requested
            if action == "fill":
                args["handle"] = meta.id
            raw = registry.dispatch("browser_vault", args, task_id="fixture")
            outcome = json.loads(raw) if isinstance(raw, str) else raw
    finally:
        unlock.set_code_prompt_callback(None)
    expected = requested is not False and configured
    assert outcome["success"] is True, outcome
    assert outcome["remembered"] is expected
    assert _checked(page, "trust") is expected
    assert page.evaluate("window.seen") == [expected]
    assert "fixture-password" not in json.dumps(outcome) and "246810" not in json.dumps(outcome)


def test_registered_schema_exposes_per_call_remember_opt_out():
    from tools import browser_vault_tool
    from tools.registry import registry

    schema = registry.get_schema("browser_vault")
    assert schema is browser_vault_tool.BROWSER_VAULT_SCHEMA
    assert schema is not None
    setting = schema["parameters"]["properties"]["remember"]
    assert setting["type"] == "boolean" and setting["default"] is True
    assert "enter_code" in setting["description"] and "Untick it first" in setting["description"]


def test_remember_setting_is_read_from_the_active_config(tmp_path, monkeypatch):
    from agent.vault_origin_aliases import remember_logins_enabled
    from hermes_cli.config import load_config_readonly

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("vault:\n  remember_logins:\n    default: true\n    work: false\n")
    assert load_config_readonly()["vault"]["remember_logins"]["work"] is False
    assert remember_logins_enabled("work") is False
    assert remember_logins_enabled("personal") is True


@pytest.mark.parametrize("setting, account, expected", [
    (None, "brianle", True),
    ({"default": True, "work": False}, "work", False),
    ({"default": True, "work": False}, "personal", True),
    ({"default": False}, None, False),
    (False, "personal", False),
    ("yes", "personal", True),  # invalid values keep the shipped default
])
def test_remember_logins_setting_per_browser_account(setting, account, expected):
    from agent import vault_origin_aliases

    vault = {} if setting is None else {"remember_logins": setting}
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": vault}):
        assert vault_origin_aliases.remember_logins_enabled(account) is expected


def test_login_fill_respects_remember_off_for_the_bound_account():
    from tools import browser_vault_tool

    with patch("tools.browser_camofox.is_camofox_mode", return_value=True), \
         patch("tools.browser_camofox.get_session_account", return_value="work"), \
         patch("hermes_cli.config.load_config_readonly",
               return_value={"vault": {"remember_logins": {"default": True, "work": False}}}):
        assert browser_vault_tool._remember_login("task") is False
