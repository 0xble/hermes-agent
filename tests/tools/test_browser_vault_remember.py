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


def test_hidden_box_is_left_alone(page):
    _open(page, inside=f'<div style="display:none">{_box("rm", "Remember me")}</div>')
    result = _fill(page)
    assert result["remembered"] is False and not _checked(page, "rm")


@pytest.mark.parametrize("label", [
    "Email me marketing offers", "Subscribe to our newsletter", "I agree to the Terms and Conditions",
    "I consent to the privacy policy", "This is a public or shared computer", "Don't remember me",
    "Remember me and send me product updates",
])
def test_decoys_are_never_ticked(page, label):
    _open(page, inside=_box("decoy", label))
    result = _fill(page)
    assert result["filled"] == 1 and result["remembered"] is False
    assert not _checked(page, "decoy")


def test_decoy_beside_remember_box_only_remember_is_ticked(page):
    _open(page, inside=_box("news", "Send me the newsletter") + _box("rm", "Remember me"))
    assert _fill(page)["remembered"] is True
    assert _checked(page, "rm") and not _checked(page, "news")


def test_box_outside_the_login_form_is_left_alone(page):
    _open(page, outside=f'<form id="other">{_box("rm", "Remember me")}</form>')
    result = _fill(page)
    assert result["remembered"] is False and not _checked(page, "rm")


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


def test_remember_off_leaves_box_alone(page):
    _open(page, inside=_box("rm", "Remember me"))
    result = _fill(page, remember=False)
    assert result["filled"] == 1 and result["remembered"] is False and not _checked(page, "rm")


def test_enter_code_ticks_trust_this_device(page):
    from agent.vault_backends import unlock
    from tools import browser_vault_tool

    page.html = ('<!doctype html><body><form><label for="otp">Verification code</label>'
                 '<input id="otp" autocomplete="one-time-code" inputmode="numeric">'
                 + _box("trust", "Trust this device") + _box("mk", "Email me security tips and offers") +
                 '<button>Verify</button></form></body>')
    page.goto("https://login.example/mfa")
    evaluate = lambda _task, expression: {"success": True, "result": page.evaluate(expression)}  # noqa: E731
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
