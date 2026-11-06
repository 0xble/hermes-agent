"""Login / checkout form control classifier for vault autofill.

Python port (~170 LOC) of Merit-Systems/OpenInstinct's
``lib/manager/server/kernel-login-autofill.ts`` (MIT). Classifies visible
input controls on a page into login-autofill tokens. The vault fill path
uses the classification to select the single best current-password control
(the identifier is agent-visible metadata and is typed by the agent
itself via normal input tools).

Scoring:
- exact autocomplete-token match ................ 100
- type=password (not new/confirm/create/repeat) .. 90
- type=email / type=tel .......................... 85
- label/name regex heuristics .................. 70-75
Hard exclusions: autocomplete ``new-password`` / ``one-time-code``, and
label/name text matching ``(new|confirm|create|repeat)\\s*password``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import itertools

LOGIN_AUTOFILL_TOKENS = ("username", "email", "tel", "current-password")

# Payment / address autocomplete tokens (WHATWG) the checkout fill targets. ``cc-exp`` (combined
# MM/YY) is derived at fill time from exp_month + exp_year. Field-name/label heuristics below back
# up sites that omit autocomplete attributes.
PAYMENT_AUTOFILL_TOKENS = ("cc-number", "cc-name", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc", "cc-type")
ADDRESS_AUTOFILL_TOKENS = ("address-line1", "address-line2", "address-level2", "address-level1",
                           "postal-code", "country-name", "country")
PROTECTED_FIELD_TOKENS = ("bday", "bday-day", "bday-month", "bday-year")
_CHECKOUT_HEURISTICS = (
    (re.compile(r"\b(?:card\s*number|cardnumber|ccnumber|cc\s*num|pan)\b"), "cc-number"),
    (re.compile(r"\b(?:name\s*on\s*card|cardholder|cc\s*name|ccname)\b"), "cc-name"),
    (re.compile(r"\b(?:cvc|cvv|csc|security\s*code|card\s*code)\b"), "cc-csc"),
    (re.compile(r"\b(?:card|credit\s*card)\s*(?:type|brand)\b"), "cc-type"),
    (re.compile(r"\b(?:exp(?:iry|iration)?\s*month|exp\s*mm|ccmonth)\b"), "cc-exp-month"),
    (re.compile(r"\b(?:exp(?:iry|iration)?\s*year|exp\s*yy(?:yy)?|ccyear)\b"), "cc-exp-year"),
    (re.compile(r"\b(?:exp(?:iry|iration)?(?:\s*date)?|mm\s*yy(?:yy)?|valid\s*thru)\b"), "cc-exp"),
    (re.compile(r"\b(?:address\s*(?:line\s*)?2|apt|suite|unit)\b"), "address-line2"),
    (re.compile(r"\b(?:address(?:\s*line\s*1)?|street)\b"), "address-line1"),
    (re.compile(r"\b(?:city|town|locality)\b"), "address-level2"),
    (re.compile(r"\b(?:state|province|region|county)\b"), "address-level1"),
    (re.compile(r"\b(?:zip|postal|postcode)\b"), "postal-code"),
    (re.compile(r"\b(?:country)\b"), "country-name"),
)

# An MM/YY placeholder does not make issue/start dates expiry targets.
_CHECKOUT_NON_EXPIRY_DATE = re.compile(r"\b(?:issue\s*date|valid\s*from|start\s*date)\b")

_EXCLUDED_AUTOCOMPLETE = {"new-password", "one-time-code"}

_RE_EXCLUDED_PASSWORD = re.compile(r"\b(?:new|confirm|create|repeat)\s*password\b")
_RE_EMAIL = re.compile(r"\b(?:e[\s-]?mail|email address)\b")
_RE_TEL = re.compile(r"\b(?:phone|telephone|mobile)\b")
_RE_USERNAME = re.compile(
    r"\b(?:user\s*name|username|login|account|member|membership|mileageplus)\b"
)


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


@dataclass(frozen=True)
class LoginControl:
    """Descriptor of a visible input control, as inspected in the page."""

    autocomplete: str
    form_index: Optional[int]
    index: int
    label: str
    name: str
    type: str
    max_length: Optional[int] = None
    empty: bool = False
    width: float = 0
    height: float = 0
    container_index: Optional[int] = None
    container_input_count: int = 0
    container_has_password: bool = False
    nearby_text: str = ""
    context_truncated: bool = False
    group_label: str = ""
    group_candidate_count: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LoginControl:
        form_index = raw.get("formIndex", raw.get("form_index"))
        max_length = raw.get("maxLength", raw.get("max_length"))
        return cls(
            autocomplete=str(raw.get("autocomplete") or ""),
            form_index=int(form_index) if form_index is not None else None,
            index=int(raw.get("index") or 0),
            label=str(raw.get("label") or ""),
            name=str(raw.get("name") or ""),
            type=str(raw.get("type") or ""),
            max_length=int(max_length) if max_length is not None else None,
            empty=raw.get("empty") is True,
            width=float(raw.get("width") or 0),
            height=float(raw.get("height") or 0),
            container_index=raw.get("containerIndex"),
            container_input_count=int(raw.get("containerInputCount") or 0),
            container_has_password=raw.get("containerHasPassword") is True,
            nearby_text=str(raw.get("nearbyText") or "")[:300],
            context_truncated=raw.get("contextTruncated") is True,
            group_label=str(raw.get("groupLabel") or "")[:180],
            group_candidate_count=int(raw.get("groupCandidateCount") or 0),
        )


@dataclass(frozen=True)
class ClassifiedLoginControl:
    control: LoginControl
    score: int
    token: str


def classify_login_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify one control, or return None if it is not a login fill target."""
    autocomplete_tokens = [
        t for t in control.autocomplete.lower().split() if t
    ]
    if any(t in _EXCLUDED_AUTOCOMPLETE for t in autocomplete_tokens):
        return None

    searchable = _normalize_text(
        " ".join(part for part in (control.name, control.label) if part)
    )
    if _RE_EXCLUDED_PASSWORD.search(searchable) or classify_otp_controls([control]):
        return None

    for token in LOGIN_AUTOFILL_TOKENS:
        if token in autocomplete_tokens:
            if token == "current-password" and control.type != "password":
                return None
            return ClassifiedLoginControl(control, 100, token)
    if control.type == "password":
        return ClassifiedLoginControl(control, 90, "current-password")
    if control.type == "email":
        return ClassifiedLoginControl(control, 85, "email")
    if control.type == "tel":
        return ClassifiedLoginControl(control, 85, "tel")
    if _RE_EMAIL.search(searchable):
        return ClassifiedLoginControl(control, 75, "email")
    if _RE_TEL.search(searchable):
        return ClassifiedLoginControl(control, 75, "tel")
    if _RE_USERNAME.search(searchable):
        return ClassifiedLoginControl(control, 70, "username")
    return None


_RE_OTP = re.compile(
    r"\b(?:one[\s-]?time|verification|security|auth(?:entication|enticator)?|2fa|two[\s-]?factor|mfa|totp|otp|"
    r"passcode|sms)\b.*\b(?:code|pin|token)\b|\b(?:otp|totp|2fa|mfa|verification\s*code|passcode)\b"
)


_RE_OTP_CONTEXT = re.compile(
    r"\b(?:verification|security\s+code|one\s+time\s+code|2fa|mfa|authenticator|"
    r"two\s+factor|codigo\s+de\s+verificacion|code\s+de\s+verification|sicherheitscode)\b"
)
_RE_OTP_DELIVERY_OR_LIFETIME = re.compile(
    r"\b(?:(?:sent|texted|delivered)\s+to\s+(?:your\s+)?(?:phone|mobile|telephone)|"
    r"expires?\s+in\s+\d+\s+(?:seconds?|minutes?))\b"
)
_RE_NOT_OTP_CONTEXT = re.compile(
    r"\b(?:card|cc|cvv|cvc|csc|expir\w*|mm\s*yy|billing|phone|mobile|telephone|"
    r"date|day|month|year|address|zip|postal|coupon|promo|voucher|gift)\b"
)


def _anonymous_otp_group(controls: List[LoginControl], code_length: Optional[int] = None) -> bool:
    """Require a complete, compact anonymous widget; never infer OTP from box count alone."""
    if not 4 <= len(controls) <= 8 or (code_length is not None and len(controls) != code_length):
        return False
    boxes = sorted(controls, key=lambda c: c.index)
    first = boxes[0]
    if (first.container_index is None or first.container_input_count != len(boxes)
            or first.container_has_password or first.context_truncated
            or first.width < 16 or first.height < 16
            or first.width > 100 or first.height > 100):
        return False
    context = _normalize_text(first.nearby_text)
    # Delivery and code lifetime describe the challenge, not the input's
    # purpose. Keep unrelated phone/date text and every card/postal veto.
    purpose_context = _RE_OTP_DELIVERY_OR_LIFETIME.sub(" ", context)
    if not _RE_OTP_CONTEXT.search(context) or _RE_NOT_OTP_CONTEXT.search(purpose_context):
        return False
    return all(
        c.type == first.type and c.type in ("text", "tel", "number")
        and c.empty and not c.name.strip() and not c.label.strip() and not c.autocomplete.strip()
        and c.container_index == first.container_index and c.form_index == first.form_index
        and c.container_input_count == len(boxes) and not c.container_has_password
        and not c.context_truncated
        and abs(c.width - first.width) <= 2 and abs(c.height - first.height) <= 2
        and _normalize_text(c.nearby_text) == context
        and (i == 0 or c.index == boxes[i - 1].index + 1)
        for i, c in enumerate(boxes)
    )


def classify_otp_controls(controls: list[LoginControl]) -> list[ClassifiedLoginControl]:
    """The controls that take a second-factor code. ``autocomplete=one-time-code`` is authoritative;
    otherwise a text/tel/number input whose name/label says code/OTP/2FA/verification. Some sites split
    the code into one input per digit (``maxlength=1`` boxes): they are returned in DOM order and the
    fill spreads the code across them."""
    out: list[ClassifiedLoginControl] = []
    for c in controls:
        tokens = c.autocomplete.lower().split()
        if "one-time-code" in tokens:
            out.append(ClassifiedLoginControl(c, 100, "one-time-code"))
            continue
        if c.type not in ("text", "tel", "number", "password", ""):
            continue
        if _RE_OTP.search(_normalize_text(" ".join(p for p in (c.name, c.label) if p))):
            out.append(ClassifiedLoginControl(c, 70, "one-time-code"))
    if out:
        return out
    groups: Dict[int, List[LoginControl]] = {}
    for c in controls:
        if c.container_index is not None:
            groups.setdefault(c.container_index, []).append(c)
    matched = [g for g in groups.values() if _anonymous_otp_group(g)]
    # Ambiguous page: never choose between two similarly shaped widgets.
    if len(matched) == 1:
        return [ClassifiedLoginControl(c, 55, "anonymous-one-time-code") for c in matched[0]]
    return []


def select_password_fill(
    classified: list[ClassifiedLoginControl],
    password: str,
) -> list[dict[str, Any]]:
    """Select the single best current-password control to fill.

    The vault fill path is password-only: the identifier is agent-visible
    metadata and is typed by the agent via normal input tools. This picks
    the highest-scoring ``current-password`` control (ties broken by DOM
    order) and returns ``[{"index": int, "token": "current-password",
    "value": password}]`` or ``[]`` when no password field exists.
    """
    passwords = [c for c in classified if c.token == "current-password"]
    if not passwords or not password:
        return []
    best_password = min(passwords, key=lambda c: (-c.score, c.control.index))
    return [
        {
            "index": best_password.control.index,
            "token": "current-password",
            "value": password,
        }
    ]


def classify_checkout_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify payment/address targets; masked passwords qualify only as CVC."""
    tokens = [t for t in control.autocomplete.lower().split() if t]
    if set(tokens) & {"one-time-code", "current-password", "new-password", "username"}:
        return None
    for token in PAYMENT_AUTOFILL_TOKENS + ADDRESS_AUTOFILL_TOKENS:
        if token in tokens:
            if control.type == "password" and token != "cc-csc":
                return None
            return ClassifiedLoginControl(control, 100, "country-name" if token == "country" else token)
    if control.type == "email":
        return None
    if (re.fullmatch(r"#{3,4}", control.label.strip())
            and control.group_candidate_count == 1
            and re.search(r"\b(?:cvc|cvv|csc|security\s*code|card\s*code)\b", _normalize_text(control.group_label))):
        return ClassifiedLoginControl(control, 65, "cc-csc")
    metadata = " ".join(part for part in (control.name, control.label) if part)
    metadata = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", metadata)
    metadata = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", metadata)
    searchable = _normalize_text(metadata)
    if (control.type in {"combobox", "listbox", "select"}
            and re.search(r"\bexp(?:iry|iration)?\b", _normalize_text(control.group_label))
            and searchable in {"month", "year"}):
        return ClassifiedLoginControl(control, 70, "cc-exp-" + searchable)
    for pattern, token in _CHECKOUT_HEURISTICS:
        if pattern.search(searchable):
            # Narrow guards adapted from upstream PRs 136639 and 134723.
            if token.startswith("cc-exp") and _CHECKOUT_NON_EXPIRY_DATE.search(searchable):
                continue
            if control.type == "password" and token != "cc-csc":
                continue
            return ClassifiedLoginControl(control, 70, token)
    if control.type in {"combobox", "listbox", "select"}:
        token = {"mm": "cc-exp-month", "yyyy": "cc-exp-year", "yy": "cc-exp-year"}.get(searchable)
        if token:
            return ClassifiedLoginControl(control, 60, token)
    return None


def _payment_brand(number: str) -> str:
    number = re.sub(r"[\s-]", "", number)
    if not re.fullmatch(r"[0-9]{13,19}", number):
        return ""
    if number.startswith("4"):
        return "visa"
    if number.startswith(("34", "37")):
        return "amex"
    if 51 <= int(number[:2]) <= 55 or 2221 <= int(number[:4]) <= 2720:
        return "mastercard"
    if number.startswith(("6011", "65")) or 644 <= int(number[:3]) <= 649:
        return "discover"
    return ""


def select_checkout_fills(classified: list[ClassifiedLoginControl], secret: dict[str, str],
                          field_tokens: dict[str, str]) -> list[dict[str, Any]]:
    """Map a payment/address secret payload onto the best control per autocomplete token.

    ``field_tokens`` is ``PAYMENT_FIELDS`` / ``ADDRESS_FIELDS`` (agent/vault_store.py). A combined
    ``cc-exp`` control gets ``MM/YY`` from exp_month + exp_year and then suppresses the separate
    month/year fills. Returns ``[{"index", "token", "value"}]``: one control per token, highest
    score then DOM order.
    """
    values: dict[str, str] = {tok: secret[f] for f, tok in field_tokens.items() if secret.get(f)}
    if brand := _payment_brand(secret.get("card_number", "")):
        values["cc-type"] = brand
    if "cc-exp-month" in values and "cc-exp-year" in values:
        values["cc-exp"] = f"{values['cc-exp-month'].zfill(2)}/{values['cc-exp-year'][-2:]}"
    fills: list[dict[str, Any]] = []
    for token, value in values.items():
        candidates = sorted((c for c in classified if c.token == token), key=lambda c: (-c.score, c.control.index))
        if candidates:
            control = candidates[0].control
            if token == "cc-exp" and "cc-exp-month" in values and re.search(r"yyyy", control.label, re.IGNORECASE):
                separator = " / " if " / " in control.label else "/"
                value = values['cc-exp-month'].zfill(2) + separator + values['cc-exp-year']
            elif token == "cc-exp-year" and control.max_length == 2:
                value = value[-2:]
            fills.append({"index": control.index, "token": token, "value": value})
    if any(f["token"] == "cc-exp" for f in fills):
        fills = [f for f in fills if f["token"] not in ("cc-exp-month", "cc-exp-year")]
    # A brand change can reset number/expiry widgets. Select it before writing
    # those fields, never clear an already-filled card by setting brand last.
    fills.sort(key=lambda f: f["token"] != "cc-type")
    return fills


_RE_BDAY = re.compile(r"\b(?:date\s*of\s*birth|birth\s*date|birthdate|dob)\b")
_DATE_PART_CONTROL_TYPES = frozenset({"select", "text", "number", "tel"})
_PROTECTED_CONTROL_TYPES = {
    "bday": frozenset({"date"}),
    "bday-month": _DATE_PART_CONTROL_TYPES,
    "bday-day": _DATE_PART_CONTROL_TYPES,
    "bday-year": _DATE_PART_CONTROL_TYPES,
}
_RE_BDAY_PART = {
    f"bday-{part}": re.compile(
        rf"\b(?:(?:birth|dob)\s*(?:date\s*)?{part}|{part}\s*(?:of\s*)?(?:birth|dob))\b"
    )
    for part in ("month", "day", "year")
}


def classify_protected_field_control(control: LoginControl, semantic: str) -> Optional[ClassifiedLoginControl]:
    """Classify one explicitly configured protected semantic.

    Standard WHATWG autocomplete tokens are authoritative. Heuristics are
    deliberately narrow: a birth date may match only a control whose own
    name or label says birth date or DOB, never a generic date control.
    """
    if semantic != "bday":
        return None
    # Control type gates every match, standard token included: a page can put
    # autocomplete="bday" on any control, and only these can show a date part.
    allowed = _PROTECTED_CONTROL_TYPES
    tokens = [t for t in control.autocomplete.lower().split() if t]
    for token in PROTECTED_FIELD_TOKENS:
        if token in tokens:
            return ClassifiedLoginControl(control, 100, token) if control.type in allowed[token] else None
    if control.type not in allowed["bday-day"] | allowed["bday"]:
        return None
    searchable = _normalize_text(" ".join(part for part in (control.name, control.label) if part))
    for token, pattern in _RE_BDAY_PART.items():
        if pattern.search(searchable):
            return ClassifiedLoginControl(control, 80, token) if control.type in allowed[token] else None
    # A heuristic combined date is safe only for a date input. Selects and
    # numeric/text controls need an explicit day/month/year meaning.
    if control.type == "date" and _RE_BDAY.search(searchable):
        return ClassifiedLoginControl(control, 80, "bday")
    return None


def select_protected_field_fills(classified: List[ClassifiedLoginControl], semantic: str,
                                 value: str) -> List[Dict[str, Any]]:
    """Select one birth-date field or a complete day/month/year split."""
    if semantic != "bday" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        return []
    year, month, day = value.split("-")
    parts = {"bday-month": month, "bday-day": day, "bday-year": year}
    # A split date is one widget only when all three controls share a real form,
    # or, outside any form, one tight container. Form-less controls with no
    # container are never grouped: None is "unknown", not a shared widget.
    def group_of(c: ClassifiedLoginControl) -> Optional[tuple]:
        if c.control.form_index is not None:
            return ("form", c.control.form_index)
        if c.control.container_index is not None:
            return ("container", c.control.container_index)
        return None

    groups = {group_of(c) for c in classified if c.token in parts} - {None}
    complete_groups = []
    for group in groups:
        selected: List[Dict[str, Any]] = []
        score = 0
        for token, part in parts.items():
            candidates = sorted(
                (c for c in classified if c.token == token and group_of(c) == group),
                key=lambda c: (-c.score, c.control.index),
            )
            if not candidates:
                break
            score += candidates[0].score
            selected.append({"index": candidates[0].control.index, "token": token, "value": part})
        if len(selected) == 3:
            complete_groups.append((score, min(f["index"] for f in selected), selected))
    if complete_groups:
        return sorted(complete_groups, key=lambda group: (-group[0], group[1]))[0][2]
    singles = sorted((c for c in classified if c.token == "bday"),
                     key=lambda c: (-c.score, c.control.index))
    return ([{"index": singles[0].control.index, "token": "bday", "value": value}]
            if singles else [])


# JS expression evaluated in the page to inspect candidate input controls.
# Ported from OpenInstinct's nativeLoginControlInspectionExpression.
# Inspection stamps every input with ``<nonce>:<index>`` under a per-inspection attribute; the fill
# script resolves targets by the stamp of ITS OWN inspection instead of re-querying by position, so
# neither a DOM reflow nor a second inspection in between can redirect the password into another field.
INSPECTION_STAMP_ATTR = "data-hermes-vault-slot"


def build_otp_fills(otp_controls: list[ClassifiedLoginControl], code: str) -> list[dict[str, Any]]:
    """One fill per box. Default: the single best-scoring code field takes the whole code.

    Per-digit entry only when the page unmistakably uses it: exactly len(code) OTP controls that are all
    ``maxlength=1``, all in the same form, and adjacent in DOM order (the classic N-box widget). Anything
    looser (several code-like inputs scattered over a page) gets ONE field, never a digit sprayed across
    unrelated inputs."""
    if not otp_controls:
        return []
    if any(c.token == "anonymous-one-time-code" for c in otp_controls):
        boxes = [c.control for c in otp_controls]
        if not all(c.token == "anonymous-one-time-code" for c in otp_controls) or not _anonymous_otp_group(boxes, len(code)):
            return []
        return [{"index": c.index, "token": "one-time-code", "value": ch}
                for c, ch in zip(sorted(boxes, key=lambda c: c.index), code)]
    best = max(otp_controls, key=lambda c: c.score)
    boxes = sorted((c for c in otp_controls if c.control.max_length == 1), key=lambda c: c.control.index)
    if (len(boxes) == len(code)
            and len({b.control.form_index for b in boxes}) == 1
            and all(b.control.index - a.control.index == 1 for a, b in itertools.pairwise(boxes))):
        return [{"index": b.control.index, "token": "one-time-code", "value": ch} for b, ch in zip(boxes, code)]
    return [{"index": best.control.index, "token": "one-time-code", "value": code}]


# Open shadow roots belong to the same document/origin. Never traverse frames:
# their origin would need a separate binding check at the mutation boundary.
_QUERY_ALL_JS = """const queryAll = (selector) => {
    const roots = new Set([document]), matches = new Set();
    // Synthetic shadow DOM can expose a root or control through multiple paths.
    // Stamp each control once: duplicate indices would overwrite its nonce binding.
    for (const root of roots) {
      for (const element of root.querySelectorAll(selector)) matches.add(element);
      for (const element of root.querySelectorAll('*')) {
        if (element.shadowRoot) roots.add(element.shadowRoot);
      }
    }
    return Array.from(matches);
  };"""


def build_form_probe_js(selector: str) -> str:
    """Use the same DOM scope for tab discovery as inspection and injection."""
    return "(() => {" + _QUERY_ALL_JS + "return queryAll(" + json.dumps(selector) + ").length > 0;})()"


def build_inspection_js(nonce: str) -> str:
    from agent.vault_checkout_controls import INSPECT_HELPERS
    return (_LOGIN_CONTROL_INSPECTION_JS_TEMPLATE.replace("__NONCE__", json.dumps(nonce))
            .replace("__PAYMENT_CONTEXT__", INSPECT_HELPERS)
            .replace("__QUERY_ALL__", _QUERY_ALL_JS))


_LOGIN_CONTROL_INSPECTION_JS_TEMPLATE = """(() => {
  const nonce = __NONCE__;
  __QUERY_ALL__
  __PAYMENT_CONTEXT__
  const elements = queryAll("input, select, [role=combobox], [role=listbox]");
  const forms = queryAll("form");
  const containerIds = new WeakMap();
  let nextContainerId = 0;
  const containerFor = (element) => {
    // A tight ancestor, not merely the page's form: never combine distant fields.
    let parent = element.parentElement;
    for (let depth = 0; parent && depth < 4; parent = parent.parentElement, depth++) {
      const count = parent.querySelectorAll("input").length;
      if (count >= 4 && count <= 8) return parent;
    }
    return null;
  };
  const contextFor = (container) => {
    if (!container) return { text: "", truncated: false };
    let heading = "";
    for (let node = container, depth = 0; node && depth < 7; node = node.parentElement, depth++) {
      const h = node.querySelector("h1, h2, h3, h4, [role=heading]");
      if (h) { heading = h.textContent || ""; break; }
    }
    const raw = [heading, container.parentElement && container.parentElement.textContent || ""].join(" ").replace(/\\s+/g, " ").trim();
    return { text: raw.slice(0, 300), truncated: raw.length > 300 };
  };
  // Wrapping labels include select options or a button's current value in
  // textContent. Those are widget state, not semantic label evidence.
  const labelText = (label) => {
    const copy = label.cloneNode(true);
    copy.querySelectorAll('input, select, textarea, button, [role=combobox], [role=listbox]')
      .forEach(control => control.remove());
    return copy.textContent || "";
  };
  elements.forEach((element, index) => element.setAttribute("data-hermes-vault-slot", nonce + ":" + index));
  const out = elements.flatMap((element, index) => {
    if (element.matches(":disabled") || element.readOnly) return [];
    if (["hidden", "submit", "button", "reset", "file", "image", "checkbox", "radio"].includes(element.type) &&
        !["combobox", "listbox"].includes(element.getAttribute("role"))) return [];
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || element.getClientRects().length === 0) return [];
    const labels = element.labels ? Array.from(element.labels, labelText) : [];
    if (!labels.length && element.parentElement &&
        element.parentElement.querySelectorAll('input, select, [role=combobox], [role=listbox]').length === 1) {
      labels.push(...Array.from(element.parentElement.children)
        .filter(n => n.tagName === "LABEL").map(labelText));
    }
    const ariaText = (element.getAttribute("aria-labelledby") || "")
      .split(/\\s+/).filter(Boolean)
      .map((id) => { const n = element.getRootNode().getElementById(id); return n ? (n.textContent || "") : ""; })
      .join(" ");
    const resolvedFormIndex = element.form ? forms.indexOf(element.form) : -1;
    const container = containerFor(element);
    if (container && !containerIds.has(container)) containerIds.set(container, nextContainerId++);
    const context = contextFor(container);
    const rect = element.getBoundingClientRect();
    const paymentContext = localPaymentContext(element);
    return [{
      groupLabel: paymentContext.label,
      groupCandidateCount: paymentContext.candidates,
      autocomplete: element.getAttribute("autocomplete") || "",
      formIndex: resolvedFormIndex >= 0 ? resolvedFormIndex : null,
      index,
      empty: element.value === "",
      width: Math.round(rect.width * 10) / 10,
      height: Math.round(rect.height * 10) / 10,
      containerIndex: container ? containerIds.get(container) : null,
      containerInputCount: container ? container.querySelectorAll("input").length : 0,
      containerHasPassword: !!container && !!container.querySelector('input[type="password"]'),
      nearbyText: context.text,
      contextTruncated: context.truncated,
      maxLength: element.maxLength > 0 ? element.maxLength : null,
      label: [
        ...labels,
        element.getAttribute("aria-label") || "",
        ariaText,
        element.getAttribute("placeholder") || "",
        element.getAttribute("title") || "",
      ].join(" "),
      name: [element.name, element.id].join(" "),
      type: element.tagName === "SELECT" ? "select" : (element.getAttribute("role") || element.type || ""),
    }];
  });
  return JSON.stringify(out);
})()"""


def build_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str = "", *,
                  verify_only: bool = False, retain_stamps: bool = False) -> str:
    """Build a JS expression that fills the selected controls and reports only a count. The
    returned expression never echoes the values back.

    ``expected_origin`` is asserted against ``window.location.origin`` synchronously inside the SAME
    evaluated script, immediately before any write. If the page navigated between inspection and fill
    (TOCTOU), the script writes nothing and returns ``{"refused": "origin_changed", "found": <actual>}``:
    proof scope equals mutation scope (#88706). Targets resolve by the ``<nonce>:<index>`` stamp of
    THIS inspection; a ``current-password`` fill additionally requires ``type=password``; ``<select>``
    controls (country, state, expiry month) match an option by value or visible text. No marker is
    left on filled controls so later model-driven DOM reads cannot address them deterministically.
    """
    from agent.vault_checkout_controls import FILL_HELPERS

    payload = json.dumps(
        [{"index": f["index"], "token": f.get("token", "current-password"), "value": f["value"]} for f in fills]
    )
    return (_FILL_JS_TEMPLATE.replace("__EXPECTED_ORIGIN__", json.dumps(expected_origin))
            .replace("__NONCE__", json.dumps(nonce))
            .replace("__QUERY_ALL__", _QUERY_ALL_JS)
            .replace("__EXCLUDED_PASSWORD__", json.dumps(_RE_EXCLUDED_PASSWORD.pattern))
            .replace("__OTP_PATTERN__", json.dumps(_RE_OTP.pattern))
            .replace("__PAYMENT_FILL_HELPERS__", FILL_HELPERS)
            .replace("__VERIFY_ONLY__", json.dumps(verify_only))
            .replace("__RETAIN_STAMPS__", json.dumps(retain_stamps))
            .replace("__FILLS__", payload))


_FILL_JS_TEMPLATE = """(async () => {
  const expectedOrigin = __EXPECTED_ORIGIN__;
  if (window.location.origin !== expectedOrigin) {
    return JSON.stringify({ refused: "origin_changed", found: window.location.origin });
  }
  const fills = __FILLS__;
  const verifyOnly = __VERIFY_ONLY__;
  const retainStamps = __RETAIN_STAMPS__;
  const nonce = __NONCE__;
  __QUERY_ALL__
  const stamped = queryAll("[data-hermes-vault-slot]");
  let filled = 0;
  const selectedOptions = [];
  const fieldResults = [];
  const norm = (t) => String(t || "").trim().toLowerCase();
  __PAYMENT_FILL_HELPERS__
  const readback = (el, f) => {
    if (!usable(el, f)) return false;
    if (['combobox', 'listbox'].includes(el.getAttribute('role'))) {
      // aria-selected on a stale option alone cannot certify a trigger reset.
      if (el.getAttribute('role') === 'listbox') {
        const options = Array.from(el.querySelectorAll('[role=option][aria-selected=true]'));
        return options.length === 1 && chooseOption(Array.from(el.querySelectorAll('[role=option]'))
          .filter(o => !o.matches(':disabled,[aria-disabled=true]')), f, true) === options[0];
      }
      // Visible brand text wins over stale framework values or ARIA state.
      if (f.token === 'cc-type') {
        const ids = (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\\s+/).filter(Boolean);
        if (ids.length !== 1) return false;
        const popup = el.getRootNode().getElementById(ids[0]);
        const options = popup && popup.getAttribute('role') === 'listbox'
          ? Array.from(popup.querySelectorAll('[role=option]')).filter(o => !o.matches(':disabled,[aria-disabled=true]')) : [];
        return !!chooseOption(options, f, true) && optionMatch(norm(el.textContent) || el.getAttribute('aria-valuetext'), f.value, f.token);
      }
      return [el.value, el.getAttribute('aria-valuetext'), el.textContent].some(t => optionMatch(t, f.value, f.token));
    }
    if (el.tagName === 'SELECT') {
      const options = Array.from(el.options).filter(o => !o.matches(':disabled'));
      const option = (f.token.startsWith('cc-exp') || f.token === 'cc-type' || f.token === 'bday-month')
        ? chooseOption(options, {...f, token: f.token === 'bday-month' ? 'cc-exp-month' : f.token}, false)
        : options.find(o => [o.value, o.textContent].some(t => norm(t) === norm(f.value) || norm(t) === norm(f.value).replace(/^0/, '')));
      return !!option && el.selectedOptions.length === 1 && el.selectedOptions[0] === option;
    }
    const canonical = value => String(value || '').replace(/[^a-z0-9]/gi, '');
    return ['cc-number', 'cc-exp'].includes(f.token)
      ? canonical(el.value) === canonical(f.value) : String(el.value) === String(f.value);
  };
  for (const f of fills) {
    const result = {token: f.token, status: "missing"};
    fieldResults.push(result);
    const el = stamped.find((n) => n.getAttribute("data-hermes-vault-slot") === nonce + ':' + f.index);
    if (verifyOnly || !el || (f.token === "current-password" && el.type !== "password")) continue;
    try {
      if (!usable(el, f)) continue;
      if (['combobox', 'listbox'].includes(el.getAttribute('role'))) {
        if (await fillAria(el, f)) { filled++; result.status = 'filled'; }
        continue;
      }
      if (el.tagName === "SELECT") {
        if (f.token.startsWith('cc-exp') || f.token === 'cc-type') {
          const option = chooseOption(Array.from(el.options).filter(o => !o.matches(':disabled')), f, false);
          if (!option || !usable(el, f)) continue;
          el.selectedIndex = option.index;
          if (!usable(el, f)) continue;
          el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
          if (!usable(el, f)) continue;
          el.dispatchEvent(new Event('change', {bubbles: true}));
          if (readback(el, f)) { filled++; result.status = 'filled'; }
          continue;
        }
        const want = norm(f.value);
        const options = Array.from(el.options);
        const same = (t) => norm(t) === want || norm(t) === want.replace(/^0/, "");
        let opt;
        if (f.token === "bday-month") {
          // Month option values are often zero-based ("3" = April): trust visible
          // text, by month name when present, and never guess from a bare value.
          const names = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
          const monthOf = (t) => names.findIndex((n) => new RegExp("(^|[^a-z])" + n).test(norm(t)));
          const named = options.filter((o) => monthOf(o.textContent) >= 0);
          if (named.length) {
            const hits = named.filter((o) => monthOf(o.textContent) === parseInt(want, 10) - 1);
            opt = hits.length === 1 ? hits[0] : undefined;
          } else {
            const byText = options.filter((o) => same(o.textContent));
            opt = byText.length === 1 ? byText[0] : undefined;
          }
        } else {
          opt = options.find((o) => [o.value, o.textContent].some(same));
        }
        if (opt && usable(el, f)) {
          el.value = opt.value;
          el.dispatchEvent(new Event("change", { bubbles: true }));
          if (!usable(el, f)) continue;
          selectedOptions.push({ token: f.token, value: opt.value, label: opt.textContent });
          filled += 1; result.status = "filled";
        }
        continue;
      }
      el.focus();
      if (!usable(el, f)) continue;
      if (f.token === "current-password") {
        const tokens = String(el.getAttribute("autocomplete") || "").toLowerCase().split(/\\s+/);
        const labels = Array.from(el.labels || []).map((l) => l.textContent).join(" ");
        const ariaText = (el.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean)
          .map((id) => { const n = el.getRootNode().getElementById(id); return n ? n.textContent : ""; }).join(" ");
        const purpose = [el.name, el.id, labels, el.getAttribute("aria-label"), ariaText, el.placeholder, el.title]
          .filter(Boolean).join(" ").normalize("NFKD").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
        const style = getComputedStyle(el);
        if (window.location.origin !== expectedOrigin || !el.isConnected || el.type !== "password" ||
            el.matches(":disabled") || el.readOnly || style.display === "none" || style.visibility === "hidden" ||
            el.getClientRects().length === 0 ||
            tokens.some((t) => ["username", "email", "tel", "new-password", "one-time-code"].includes(t)) ||
            new RegExp(__EXCLUDED_PASSWORD__).test(purpose) || new RegExp(__OTP_PATTERN__).test(purpose)) continue;
      }
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      // one-time-code split into single-character boxes: f.value is the slice for THIS box (see build_otp_fills)
      if (!usable(el, f)) continue;
      if (setter && setter.set) { setter.set.call(el, f.value); } else { el.value = f.value; }
      if (!usable(el, f)) continue;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText" }));
      if (!usable(el, f)) continue;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      if (usable(el, f) && String(el.value).replace(/[^a-z0-9]/gi, "") === String(f.value).replace(/[^a-z0-9]/gi, "")) {
        filled += 1; result.status = "filled";
      }
    } catch (e) { /* skip */ }
  }
  // Let queued framework input/change reactions settle, then certify the final
  // state of EVERY target, not the historical success of each assignment.
  await new Promise(resolve => setTimeout(resolve, 50));
  filled = 0;
  fills.forEach((f, index) => {
    const el = stamped.find(n => n.getAttribute('data-hermes-vault-slot') === nonce + ':' + f.index);
    const confirmed = (verifyOnly || fieldResults[index].status === 'filled') && readback(el, f);
    fieldResults[index].status = confirmed ? 'filled' : 'missing';
    if (confirmed) filled++;
  });
  if (!retainStamps) stamped.forEach(n => n.removeAttribute('data-hermes-vault-slot'));
  return JSON.stringify({ filled, selectedOptions, fieldResults });
})()"""
