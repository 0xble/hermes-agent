"""One-way retirement of native review-status messages from older gateway builds."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from gateway.config import Platform
from gateway.session import SessionSource
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _path(home=None) -> Path:
    return Path(home or get_hermes_home()) / "cache" / "review-statuses.json"


def _source(item):
    data = item.get("source") if isinstance(item, dict) else None
    if not isinstance(data, dict) or data.get("platform") != Platform.TELEGRAM.value or not data.get("chat_id"):
        return None
    try:
        return SessionSource(**{**data, "platform": Platform.TELEGRAM})
    except (TypeError, ValueError):
        return None


async def retire_legacy_review_statuses(runner, *, home=None) -> None:
    """Delete tracked native review notices and retain only rows needing a later retry.

    This is migration-only: new reviews never create this file or status message.
    """
    path = _path(home)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(items, dict):
        return

    remaining = {}
    for key, item in items.items():
        message_id = item.get("message_id") if isinstance(item, dict) else None
        if not message_id:
            continue
        source = _source(item)
        adapter = runner._adapter_for_source(source) if source is not None else None
        if source is None or adapter is None:
            remaining[key] = item
            continue
        try:
            delete_status = getattr(type(adapter), "_delete_status_message", None)
            deleted = await (delete_status(adapter, source.chat_id, str(message_id)) if delete_status else
                             adapter.delete_message(source.chat_id, str(message_id)))
        except Exception:
            logger.debug("Legacy review-status migration deletion failed", exc_info=True)
            deleted = False
        # ``None`` means the platform's local gate did not attempt deletion;
        # preserve the row for a later connected gateway rather than claiming it is gone.
        if not bool(getattr(deleted, "success", deleted)):
            remaining[key] = item

    if remaining:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
