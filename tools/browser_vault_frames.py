"""Document-bound processor-frame access over the existing Camofox evaluate API.

Private vault helper, not a model-visible tool. No backend fallback, subprocess,
argv, or transport exception text: expressions may contain vault material.
"""
from __future__ import annotations

import threading
from urllib.parse import urlsplit

from hermes_constants import hermes_home_key

_bindings: dict[tuple, frozenset[tuple[str, str, str]]] = {}
_bindings_lock = threading.Lock()

PAYMENT_FRAME_ORIGINS = frozenset({
    "https://js.stripe.com",
    "https://assets.braintreegateway.com",
    "https://checkoutshopper-live.adyen.com",
    "https://checkoutshopper-test.adyen.com",
})
_ERROR = "Payment processor frame access refused or unavailable; rediscover before retrying."


class PaymentFrameError(RuntimeError):
    """A sanitized failure. Discovery errors must not become an empty success."""


def _exact_origin(value: str) -> str:
    if not isinstance(value, str):
        raise PaymentFrameError(_ERROR)
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or value != f"{parsed.scheme}://{parsed.netloc}"):
        raise PaymentFrameError(_ERROR)
    return value


def _evaluate(task_id: str, body: dict) -> tuple[object, tuple]:
    from tools import browser_camofox as backend

    if not backend.is_camofox_mode():
        raise PaymentFrameError(_ERROR)
    url = urlsplit(backend.get_camofox_url())
    if (url.username or url.password or url.query or url.fragment
            or not (url.scheme == "https" or
                    (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise PaymentFrameError(_ERROR)
    session = backend._get_session(task_id)
    tab_id = session.get("tab_id")
    user_id = session.get("user_id")
    if not tab_id or not user_id:
        raise PaymentFrameError(_ERROR)
    key = (hermes_home_key(), backend.get_camofox_url(), task_id, user_id, tab_id)
    binding = (body.get("frameId"), body["merchantOrigin"], body.get("frameOrigin"))
    with _bindings_lock:
        if body["paymentOperation"] == "discover":
            # A failed rediscovery must not retain write eligibility.
            for old_key in list(_bindings):
                if old_key[:3] == key[:3]:
                    _bindings.pop(old_key, None)
        elif binding not in _bindings.get(key, frozenset()):
            raise PaymentFrameError(_ERROR)
    payload = {**body, "userId": user_id, "secret": True}
    response = backend._post(backend._tab_path({"tab_id": tab_id}, "evaluate"), body=payload, allow_redirects=False)
    if not isinstance(response, dict) or response.get("ok") is not True or "result" not in response:
        raise PaymentFrameError(_ERROR)
    # Never migrate the result to a different selected task tab mid-request.
    current = backend._get_session(task_id)
    if current.get("tab_id") != tab_id or current.get("user_id") != user_id:
        raise PaymentFrameError(_ERROR)
    return response["result"], key


def discover_payment_frames(task_id: str, merchant_origin: str) -> list[dict[str, str]]:
    """Return opaque document-bound direct processor children of the task's tab.

    Raises PaymentFrameError on capability, origin, or transport failure. [] is a
    successful discovery with no qualifying frames, never an error fallback.
    """
    try:
        _exact_origin(merchant_origin)
        frames, key = _evaluate(task_id, {"paymentOperation": "discover", "merchantOrigin": merchant_origin})
        if not isinstance(frames, list):
            raise PaymentFrameError(_ERROR)
        result = []
        seen = set()
        for frame in frames:
            if (not isinstance(frame, dict) or not isinstance(frame.get("frame_id"), str)
                    or not frame["frame_id"] or frame["frame_id"] in seen
                    or frame.get("origin") not in PAYMENT_FRAME_ORIGINS):
                raise PaymentFrameError(_ERROR)
            seen.add(frame["frame_id"])
            result.append({"frame_id": frame["frame_id"], "origin": frame["origin"]})
        with _bindings_lock:
            _bindings[key] = frozenset((frame["frame_id"], merchant_origin, frame["origin"]) for frame in result)
        return result
    except Exception:  # health: allow BLE001 - transport exceptions may retain secret request bodies; return only fixed errors
        # Deliberately no traceback/logging/chained requests exception; it may
        # retain the secret-bearing HTTP request body or expression.
        raise PaymentFrameError(_ERROR) from None


def evaluate_payment_frame(task_id: str, frame_id: str, expression: str,
                           merchant_origin: str, frame_origin: str,
                           secret: bool = False) -> dict:
    """Evaluate only in a discovered document; server enforces both identities.

    secret is accepted for caller parity. All frame operations always use secret
    transport semantics, including inspection, so mistakes cannot enable logs.
    """
    try:
        _exact_origin(merchant_origin)
        if (frame_origin not in PAYMENT_FRAME_ORIGINS or not isinstance(frame_id, str)
                or not frame_id or not isinstance(expression, str) or not expression.strip()):
            raise PaymentFrameError(_ERROR)
        result, _ = _evaluate(task_id, {
            "paymentOperation": "evaluate", "frameId": frame_id, "expression": expression,
            "merchantOrigin": merchant_origin, "frameOrigin": frame_origin,
        })
        return {"success": True, "result": result, "error": None}
    except Exception:  # health: allow BLE001 - transport exceptions may retain secret request bodies; return only fixed errors
        return {"success": False, "result": None, "error": _ERROR}
