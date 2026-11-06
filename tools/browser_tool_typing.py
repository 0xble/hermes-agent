"""Typed-input registration for both browser transports, using the vault redaction owner."""

import re
from typing import Any, Callable

from agent.display import redact_browser_typed_text_for_display, redact_tool_args_for_display
from agent.redact import register_vault_card_component, register_vault_redaction_value
from tools import browser_tool_snapshot as snapshot

def _typed_card_parts(token: str, text: str) -> list[tuple[str, str]]:
    """Validate only the format expected by the already-classified input."""
    if not text.isascii():
        return []
    if token == "cc-csc" and text.isdigit() and len(text) in (3, 4):
        return [("cvc", text)]
    if token == "cc-exp-month" and re.fullmatch(r"0?[1-9]|1[0-2]", text):
        return [("exp_month", text)]
    if token == "cc-exp-year" and text.isdigit() and len(text) in (2, 4):
        return [("exp_year", text)]
    if token == "cc-exp":
        match = re.fullmatch(r"(0?[1-9]|1[0-2])\s*/\s*(\d{2}|\d{4})", text)
        if match:
            return [("expiry", text), ("exp_month", match[1]), ("exp_year", match[2])]
    return []


def _typed_card_token(ref: str, tree: str) -> str:
    from agent.vault_login_classifier import LoginControl, classify_checkout_control

    target = re.compile(r"\[(?:ref=)?@?" + re.escape(ref.lstrip("@")) + r"\]")
    for line in tree.splitlines():
        if not target.search(line) or not re.search(r"^\s*-?\s*(?:textbox|spinbutton|combobox|select)\b", line):
            continue
        label = re.search(r'"([^"\n]*)"', line)
        if label:
            control = LoginControl.from_dict({"label": label[1], "type": "text"})
            classified = classify_checkout_control(control)
            return classified.token if classified else ""
    return ""


def register_typed_card_value(ref: str, text: str, task: str,
                              read_snapshot: Callable[[], str]) -> str:
    """Register before injection, including attempts whose backend later reports failure.

    A PAN has a distinctive shape; CVC/expiry do not. Short components require an
    existing checkout-classified field in this tab's current accessibility tree and
    stay on the tab/origin-scoped registry until the existing browser cleanup.
    """
    digits = re.sub(r"[\s-]", "", text)
    if digits.isascii() and digits.isdigit() and 13 <= len(digits) <= 19:
        register_vault_redaction_value(digits)
        return ""
    if not (text.isascii() and re.fullmatch(r"[0-9 /]{1,12}", text)):
        return ""
    try:
        tree = read_snapshot()
        if not isinstance(tree, str):
            return ""
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        # Snapshot capability failures must not turn a previously usable fill into a refusal.
        return ""
    parts = _typed_card_parts(_typed_card_token(ref, tree), text)
    if not parts:
        return ""
    from tools.browser_vault_tool import _current_page_origin
    try:
        origin = _current_page_origin(task) or ""
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        origin = ""
    for field, value in parts:
        register_vault_card_component(field, value, tab=task, origin=origin)
        if field == "exp_month":
            for variant in (str(int(value)), value.zfill(2)):
                register_vault_card_component(field, variant, tab=task, origin=origin)
        if field == "exp_year" and len(value) == 4:
            register_vault_card_component(field, value[-2:], tab=task, origin=origin)
    return origin


def typed_result(value: Any, text: str, task: str, origin: str) -> Any:
    """Keep existing token display masks, then apply registered card-value masking."""
    return snapshot._redact_browser_output(
        redact_browser_typed_text_for_display(value, text), vault_tab=task, vault_origin=origin)


def type_with_agent_browser(ref: str, text: str, task_id: str | None) -> str:
    from tools import browser_tool as bt
    from tools import browser_tool_session as session

    task = bt._last_session_key(task_id or "default")
    blocked = bt._blocked_private_page_action(task, "type")
    if blocked is not None:
        return blocked
    ref = bt._at_ref(ref)
    origin = register_typed_card_value(ref, text, task, lambda: session._run_browser_command(
        task, "snapshot", ["-c"]).get("data", {}).get("snapshot", ""))
    result = session._run_browser_command(task, "fill", [ref, text])
    display_text = (redact_tool_args_for_display("browser_type", {"text": text}) or {})["text"]
    response = ({"success": True, "typed": display_text, "element": ref} if result.get("success")
                else bt._err(result.get("error", f"Failed to type into {ref}")))
    return bt._dumps(typed_result(bt._lp._copy_fallback_warning(response, result), text, task, origin))
