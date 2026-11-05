"""Private payment fill planning and redacted, field-level receipts.

Page and processor descriptors share one selector. Ambiguity is global across
eligible documents: never send the same secret to two competing controls.
"""
from __future__ import annotations

import json
import secrets
from collections import Counter

from agent.vault_login_classifier import (
    LoginControl, build_fill_js, build_inspection_js, classify_checkout_control,
    select_checkout_fills,
)
from agent.vault_store import PAYMENT_FIELDS
from tools.browser_vault_frames import PaymentFrameError, discover_payment_frames, evaluate_payment_frame

REQUIRED = ("cc-number", "cc-exp-month", "cc-exp-year", "cc-csc")


def _parse(value):
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value


def inspect_frames(task_id: str, origin: str) -> tuple[list[dict], str | None]:
    from tools import browser_camofox

    if not browser_camofox.is_camofox_mode():
        return [], None
    try:
        frames = discover_payment_frames(task_id, origin)
    except PaymentFrameError:
        return [], "Processor-frame inspection unavailable; update the Camofox companion before retrying embedded fields."
    contexts = []
    for frame in frames:
        nonce = secrets.token_hex(8)
        inspected = evaluate_payment_frame(task_id, frame["frame_id"], build_inspection_js(nonce), origin, frame["origin"])
        raw = _parse(inspected.get("result")) if inspected.get("success") else None
        if not isinstance(raw, list):
            return [], "Processor frame changed or could not be inspected."
        controls = [LoginControl.from_dict(c) for c in raw if isinstance(c, dict)]
        classified = [c for control in controls if (c := classify_checkout_control(control)) is not None]
        contexts.append({**frame, "nonce": nonce, "controls": controls, "classified": classified})
    return contexts, None


def _candidates(contexts: list[dict]) -> tuple[dict, set[str]]:
    candidates = {}
    ambiguous = set()
    for index, context in enumerate(contexts):
        for control in context["classified"]:
            candidates.setdefault(control.token, []).append((index, control))
        for control in context.get("controls", []):
            if getattr(control, "group_candidate_count", 0) > 1 and control.label.strip() in {"###", "####"}:
                ambiguous.add("cc-csc")
    return candidates, ambiguous


def _unique_controls(candidates: dict, ambiguous: set[str]) -> dict:
    chosen = {}
    for token, matches in candidates.items():
        best = max(control.score for _, control in matches)
        strongest = [(index, control) for index, control in matches if control.score == best]
        if len(strongest) != 1:
            ambiguous.add(token)
        else:
            chosen[token] = strongest[0]
    if "cc-exp" in ambiguous:
        ambiguous.update({"cc-exp-month", "cc-exp-year"})
    return chosen


def plan(contexts: list[dict], secret: dict) -> tuple[list[dict], set[str]]:
    candidates, ambiguous = _candidates(contexts)
    chosen = _unique_controls(candidates, ambiguous)
    selected = []
    for index, context in enumerate(contexts):
        controls = [control for token, (target, control) in chosen.items()
                    if target == index and token not in ambiguous]
        fills = select_checkout_fills(controls, secret, PAYMENT_FIELDS)
        selected.append({**context, "fills": fills})
    # Prefer a unique combined expiry over a split widget, never write both.
    if any(f["token"] == "cc-exp" for context in selected for f in context["fills"]):
        for context in selected:
            context["fills"] = [f for f in context["fills"] if f["token"] not in {"cc-exp-month", "cc-exp-year"}]
    return selected, ambiguous


def receipt(filled_tokens: set[str], ambiguous: set[str], filled_count: int = 0, error: str | None = None,
            expected_tokens: set[str] | None = None) -> dict:
    covered = set(filled_tokens)
    if "cc-exp" in covered:
        covered.update({"cc-exp-month", "cc-exp-year"})
    required = set(REQUIRED) | ((expected_tokens or set()) - {"cc-exp"})
    missing = sorted(required - covered)
    statuses = {token: ("ambiguous" if token in ambiguous else "filled" if token in covered else "missing")
                for token in sorted(required | covered | ambiguous)}
    return {"success": not missing and not ambiguous and error is None,
            "partial": bool(missing or ambiguous or error), "filled_fields": filled_count,
            "fields": sorted(filled_tokens), "missing_fields": missing,
            "ambiguous_fields": sorted(ambiguous), "field_results": statuses,
            **({"error_type": "partial_fill", "error": error or "Required payment fields remain missing or ambiguous.",
                "next_step": "Vault fill remains the first route. Resolve the named missing fields using the authorized typed-entry fallback; do not repeat confirmed fields."}
               if missing or ambiguous or error else {})}


def fill(task_id: str, origin: str, selected: list[dict], ambiguous: set[str], eval_secret,
         inspection_error: str | None = None) -> dict:
    # Ambiguous sensitive targets are never partially written. The caller may
    # resolve the ambiguity through an authorized typed fallback instead.
    if ambiguous:
        return receipt(set(), ambiguous)
    filled_tokens = set()
    count = 0
    error = None
    for context in selected:
        fills = context["fills"]
        if not fills:
            continue
        expression = build_fill_js(fills, expected_origin=context["origin"], nonce=context["nonce"])
        try:
            response = (evaluate_payment_frame(task_id, context["frame_id"], expression, origin, context["origin"], secret=True)
                        if context.get("frame_id") else eval_secret(task_id, expression))
        except Exception:
            response = {"success": False}
        if not response.get("success"):
            error = "Payment write refused or unavailable; fields confirmed earlier remain filled."
            break
        result = _parse(response.get("result"))
        if not isinstance(result, dict) or result.get("refused"):
            error = "Payment document changed before writing; rediscover the checkout."
            break
        # Counts alone are insufficient: only per-field post-event readback can
        # certify a field. Never infer every target succeeded from filled > 0.
        expected = Counter(f["token"] for f in fills)
        for field in result.get("fieldResults", []):
            if isinstance(field, dict) and field.get("status") == "filled" and expected[field.get("token")] > 0:
                filled_tokens.add(field["token"])
                expected[field["token"]] -= 1
                count += 1
    # A capability failure is material only when required fields remain missing.
    if inspection_error and not set(REQUIRED) <= (filled_tokens | ({"cc-exp-month", "cc-exp-year"} if "cc-exp" in filled_tokens else set())):
        error = error or inspection_error
    expected_tokens = {field["token"] for context in selected for field in context["fills"]}
    return receipt(filled_tokens, ambiguous, count, error, expected_tokens)


def register_redaction(secret: dict, tab: str, origin: str, register_secret) -> None:
    from agent.redact import register_vault_card_component

    # A manager can store a formatted PAN; register its canonical digits
    # too, so the redactor handles whatever separator the page adds/removes.
    number = secret.get("card_number", "")
    canonical_number = "".join(ch for ch in number if ch.isascii() and ch.isdigit())
    if (13 <= len(canonical_number) <= 19
            and all((ch.isascii() and ch.isdigit()) or ch.isspace() or ch == "-" for ch in number)):
        register_secret(canonical_number)
    for field in ("card_number", "cvc"):
        register_secret(secret.get(field, ""))
    for field in ("cardholder_name", "exp_month", "exp_year", "billing_postal_code"):
        value = secret.get(field, "")
        register_vault_card_component(field, value, tab=tab, origin=origin)
        if field == "exp_month" and value.isdigit():
            for variant in (str(int(value)), f"{int(value):02d}"):
                register_vault_card_component(field, variant, tab=tab, origin=origin)
        if field == "exp_year" and value.isdigit() and len(value) == 4:
            register_vault_card_component(field, value[-2:], tab=tab, origin=origin)
