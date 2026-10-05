"""Out-of-band Telegram alert for guardian outcomes the gateway cannot report while it is down.

It talks to the Bot API directly, never through the gateway, and honors the profile's persisted
Telegram flood deadline so an outage alert cannot extend a penalty the gateway is waiting out.
The token is resolved the way the gateway resolves it and never appears in output or receipts.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request

API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10
# Outcomes worth retrying on a later tick: nothing reached Telegram.
DEFERRED = frozenset({"flood"})


def resolve_target(home: Path) -> tuple[str, str, str | None] | None:
    """(token, chat_id, thread_id) for the profile's Telegram home channel, or None."""
    from hermes_cli.env_loader import load_hermes_dotenv
    from gateway.config import Platform, load_gateway_config
    load_hermes_dotenv(hermes_home=home)
    config = load_gateway_config()
    platform = config.platforms.get(Platform.TELEGRAM)
    home_channel = config.get_home_channel(Platform.TELEGRAM)
    token = str(getattr(platform, "token", None) or "").strip()
    if not token or home_channel is None or not str(home_channel.chat_id).strip():
        return None
    return token, str(home_channel.chat_id).strip(), home_channel.thread_id


def _flood_remaining(home: Path, chat_key: str) -> float:
    from plugins.platforms.telegram import flood_state
    try:
        return max(flood_state.remaining_seconds(home, chat_key),
                   flood_state.fallback_remaining_seconds(home, chat_key))
    except (OSError, ValueError, sqlite3.Error):
        return 60.0  # An unreadable deadline is not evidence the penalty expired.


def _record_flood(home: Path, chat_key: str, wait: float) -> None:
    """Share the penalty with the gateway, which reads the same profile store on its next send."""
    from plugins.platforms.telegram import flood_state
    try:
        flood_state.record_deadline(home, chat_key, wait)
    except (OSError, ValueError, sqlite3.Error):
        try:
            flood_state.record_fallback_deadline(home, chat_key, wait)
        except (OSError, ValueError):
            pass


def _post(url: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload


def _text(home: Path, payload: dict) -> str:
    lines = [f"Hermes gateway guardian: {payload.get('outcome')} ({payload.get('action')})",
             f"reason: {payload.get('reason') or 'none'}",
             f"home: {home}", f"at: {payload.get('at')}"]
    if payload.get("label"):
        lines.insert(2, f"label: {payload['label']}")
    return "\n".join(lines)


def flood_active(home: Path, chat_key: object) -> bool:
    """Whether a chat named by an earlier attempt is still inside its flood window."""
    return isinstance(chat_key, str) and bool(chat_key) and _flood_remaining(Path(home), chat_key) > 0


def notify(home: Path, payload: dict, *, resolve=resolve_target, post=_post) -> str:
    """Send one alert. Returns sent, unconfigured, flood or failed:<kind>; never raises.

    The targeted chat (an id, not a credential) is recorded in ``payload["notify_chat"]`` so a
    flood-deferred retry can wait out the deadline without resolving credentials every tick."""
    try:
        return _notify(Path(home), payload, resolve, post)
    except Exception as exc:  # noqa: BLE001 - alerting must not break the guardian tick; the
        return f"failed:{type(exc).__name__}"  # type name only, since str(exc) could carry the URL


def _notify(home: Path, payload: dict, resolve, post) -> str:
    target = resolve(home)
    if target is None:
        return "unconfigured"
    token, chat_id, thread_id = target
    from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id
    chat_key = str(normalize_telegram_chat_id(chat_id))
    payload["notify_chat"] = chat_key
    if _flood_remaining(home, chat_key) > 0:
        return "flood"
    body = {"chat_id": normalize_telegram_chat_id(chat_id), "text": _text(home, payload),
            "disable_web_page_preview": True}
    if thread_id and str(thread_id).lstrip("-").isdigit():
        body["message_thread_id"] = int(thread_id)
    status, response = post(f"{API}/bot{token}/sendMessage", body)
    if status == 200 and response.get("ok"):
        return "sent"
    retry_after = (response.get("parameters") or {}).get("retry_after")
    if status == 429 and type(retry_after) in (int, float) and retry_after > 0:
        _record_flood(home, chat_key, float(retry_after))
        return "flood"
    return f"failed:http-{status}"
