"""Session-owned lifecycle for deferred TUI/Desktop MoA one-shots."""

from __future__ import annotations

import contextlib
import uuid
from typing import Any, Iterator

_PENDING_KEY = "pending_moa"
_PENDING = "pending"
_CLAIMED = "claimed"
_CONSUMED = "consumed"
_CANCELLED = "cancelled"


def _lock(session: dict):
    value = session.get("history_lock")
    return value if value is not None else contextlib.nullcontext()


@contextlib.contextmanager
def locked(session: dict) -> Iterator[None]:
    with _lock(session):
        yield


def _records(session: dict, *, create: bool = False) -> dict[str, dict]:
    value = session.get(_PENDING_KEY)
    if isinstance(value, dict):
        return value
    # Read old PR snapshots without letting them become a second live authority.
    if isinstance(value, list):
        migrated = {
            str(item.get("queue_token")): {
                **item,
                "token": str(item.get("queue_token")),
                "status": item.get("status") or _PENDING,
            }
            for item in value
            if isinstance(item, dict) and item.get("queue_token")
        }
        if migrated or create:
            session[_PENDING_KEY] = migrated
        return migrated
    if create:
        session[_PENDING_KEY] = {}
        return session[_PENDING_KEY]
    return {}


def create(session: dict, *, preset: str, restore: dict, prompt: str) -> dict:
    token = uuid.uuid4().hex
    record = {
        "token": token,
        "preset": str(preset),
        "restore": dict(restore),
        "prompt": prompt,
        "status": _PENDING,
    }
    with locked(session):
        _records(session, create=True)[token] = record
    return record


def get(session: dict, token: str) -> dict | None:
    if not token:
        return None
    with locked(session):
        record = _records(session).get(str(token))
        return record if isinstance(record, dict) else None


def claim(session: dict, token: str) -> dict | None:
    """Claim once; an existing claim may be resumed by an inline fallback or host child."""
    if not token:
        return None
    with locked(session):
        record = _records(session).get(str(token))
        if not isinstance(record, dict):
            return None
        status = record.get("status")
        if status == _PENDING:
            record["status"] = _CLAIMED
            return record
        if status == _CLAIMED:
            return record
        return None


def install(session: dict, record: dict) -> dict:
    token = str(record.get("token") or "")
    if not token:
        raise ValueError("pending MoA record has no token")
    copy = dict(record)
    copy["token"] = token
    with locked(session):
        _records(session, create=True)[token] = copy
    return copy


def consume(session: dict, token: str) -> bool:
    with locked(session):
        record = _records(session).get(str(token))
        if not isinstance(record, dict) or record.get("status") != _CLAIMED:
            return False
        record["status"] = _CONSUMED
        return True


def cancel(session: dict, token: str) -> bool:
    with locked(session):
        record = _records(session).get(str(token))
        if not isinstance(record, dict) or record.get("status") in {_CONSUMED, _CANCELLED}:
            return False
        record["status"] = _CANCELLED
        return True


def cancel_all(session: dict) -> int:
    with locked(session):
        count = 0
        for record in _records(session).values():
            if isinstance(record, dict) and record.get("status") in {_PENDING, _CLAIMED}:
                record["status"] = _CANCELLED
                count += 1
        return count


def mark_host_result(session: dict, token: str, *, success: bool) -> None:
    if success:
        consume(session, token)
    else:
        cancel(session, token)


def status(record: dict | None) -> str | None:
    return record.get("status") if isinstance(record, dict) else None


__all__ = [
    "cancel",
    "cancel_all",
    "claim",
    "consume",
    "create",
    "get",
    "install",
    "locked",
    "mark_host_result",
    "status",
]
