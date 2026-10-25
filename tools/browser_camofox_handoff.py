"""Negotiate Camofox's legacy no-park lifecycle without widening parked actions."""

import requests


def post_lifecycle(path: str, body: dict, *, release: bool, timeout: int) -> dict:
    from tools import browser_camofox as cf

    try:
        return cf._post(path, body, timeout=timeout)
    except requests.HTTPError as exc:
        response = exc.response
        if response is None or response.status_code != 409:
            raise
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise exc
        code = "context_close_required" if release else "context_restart_required"
        if not isinstance(payload, dict) or payload.get("code") != code:
            raise
    # Preserve pre-parking handoff semantics only when the server explicitly
    # requires a whole-context transition. Never send all=True to a parked release.
    consent = {"all": True} if release else {**body, "allowRestart": True}
    data = cf._post(path, consent, timeout=timeout)
    if data.get("ok") is True and data.get("released" if release else "restarted") is True:
        data["warning"] = ("No parking display: this identity's live tabs and in-memory page state "
                           "were discarded by the legacy browser transition. Logins persist.")
    return data
