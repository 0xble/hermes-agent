"""Private typed-input receipts for the shared local gateway.

The native interactive process publishes its live receipt beside the existing
ingress stores. Client JSON carries only its ordinary submission ID. Only a
native-bootstrap connection may acquire this record, and the admission commits
it before the client removes the transient file.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
from pathlib import Path

from agent.source_origin import native_origin
from agent.source_origin_worker import pack_origin, unpack_origin

logger = logging.getLogger(__name__)

_input = ContextVar("native_input_origin", default=None)


def _path(home, session, request):
    key = hashlib.sha256(json.dumps([session, request]).encode()).hexdigest()
    return Path(home) / "cache" / "native-input" / (key + ".json")


@contextmanager
def publish_input(home, session, request, message, text):
    receipt = native_origin((message or {}).get("display_metadata"))
    path = None
    if (receipt is not None and receipt.status == "verified_human" and receipt.platform in {"cli", "tui"}
            and receipt.text == text and receipt.event == (message or {}).get("platform_message_id")):
        from hermes_dir_acl import make_user_dir
        from utils import atomic_json_write
        path = _path(home, session, request)
        try:
            make_user_dir(path.parent, mode=0o700, parents=True, exist_ok=True)
            atomic_json_write(path, {"session": session, "request": request, "origin": pack_origin(receipt)}, mode=0o600)
        except OSError as exc:
            logger.warning("Native input origin unavailable: %s", type(exc).__name__)
    try:
        yield
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Native input origin cleanup unavailable: %s", type(exc).__name__)


@contextmanager
def acquire_input(connection, ref, params):
    receipt = None
    request = params.get("submission_id") or params.get("input_id")
    if getattr(connection, "native_owner", False) is True and isinstance(request, str):
        home = Path(connection.authority.db.db_path).parent
        path = _path(home, ref.session_id, request)
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError):
            record = {}
        if isinstance(record, dict) and record.get("session") == ref.session_id and record.get("request") == request:
            candidate = unpack_origin(record.get("origin"))
            if candidate is not None and candidate.text == params.get("text"):
                receipt = candidate
    token = _input.set((ref.session_id, request, receipt))
    try:
        yield
    finally:
        _input.reset(token)


def admission_input(request, existing=None):
    bound = _input.get()
    if bound is not None and bound[:2] == (request.ref.session_id, request.request_id):
        packed = pack_origin(bound[2])
        if packed:
            return {"source_origin_v1": packed}
    if isinstance(existing, dict):
        payload = existing.get("payload") or {}
        if payload.get("text") == request.payload.get("text") and payload.get("source_origin_v1"):
            return {"source_origin_v1": payload["source_origin_v1"]}
    return {}


def restore_input(value, text, event):
    receipt = unpack_origin(value)
    if receipt is None or receipt.text != text or not isinstance(event, str) or not event:
        return None
    from dataclasses import replace
    return replace(receipt, event=str(event))
