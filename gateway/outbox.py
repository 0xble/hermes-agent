"""Per-profile admission and user-visible egress receipts for the opt-in gateway outbox.

The store does not retry an interrupted model turn. A dispatch that crossed the
transport boundary without a receipt is quarantined, never treated as unsent.
"""

from __future__ import annotations

import asyncio
import contextvars
import errno
import functools
import inspect
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Context is copied into child tasks; only the task explicitly bound to ingress
# may use the turn's outbox. A separate background notification is not its egress.
_CURRENT_TURN: contextvars.ContextVar[tuple[Path, str, asyncio.Task | None] | None] = contextvars.ContextVar(
    "gateway_outbox_turn", default=None)
_BYPASS: contextvars.ContextVar[bool] = contextvars.ContextVar("gateway_outbox_transport", default=False)
_TURN_LOCKS: weakref.WeakValueDictionary[tuple[Path, str], asyncio.Lock] = weakref.WeakValueDictionary()
_STORES: dict[Path, "Outbox"] = {}
_STORES_LOCK = threading.Lock()
# One coalesced deferred sweep per (store, platform, profile): a multi-hour flood penalty gives
# every deferred row the same deadline, and a task per row woke hundreds of full recover() passes
# at once (2026-10-05). The event lets a newly scheduled earlier deadline wake the sleeping sweep.
_SWEEPS: dict[tuple, tuple[asyncio.Task, asyncio.Event]] = {}
# recover() is serialized per store within each event loop; a held row is logged once per process,
# and a row this process is dispatching right now is in flight, not held.
_RECOVER_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary())
_IN_FLIGHT: set[tuple[Path, str]] = set()
_HELD_LOGGED: set[tuple[Path, str]] = set()
# Local store failures (descriptor exhaustion, a briefly unopenable file) are retried before the
# dispatch is given up: the outcome of a send is known and must not be lost to a transient open.
_STORE_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
_SWEEP_IDLE_FLOOR_SECS = 1.0
_SWEEP_ERROR_BACKOFF_SECS = (5.0, 15.0, 30.0, 60.0)
_DESCRIPTOR_ERRNOS = {errno.EMFILE, errno.ENFILE}


def store_for(home: Path) -> "Outbox":
    key = Path(home).resolve()
    with _STORES_LOCK:
        if key not in _STORES:
            _STORES[key] = Outbox(key)
        return _STORES[key]


def bind_turn(home: Path, turn_id: str) -> None:
    _CURRENT_TURN.set((Path(home), turn_id, asyncio.current_task()))


def bind_event_turn(event) -> contextvars.Token:
    """Re-enter the admitted turn for adapter delivery after the runner returns."""
    token = scoped_turn_entry()
    turn_id = getattr(event, "_outbox_turn_id", None)
    home = getattr(event, "_outbox_home", None)
    if turn_id and home is not None and not getattr(event, "_outbox_duplicate", False):
        bind_turn(home, turn_id)
    return token


def clear_turn() -> None:
    _CURRENT_TURN.set(None)


def scoped_turn_entry():
    """Hide an inherited turn while processing another ingress; restore on return."""
    return _CURRENT_TURN.set(None)


def restore_turn(token: contextvars.Token) -> None:
    _CURRENT_TURN.reset(token)


async def run_turn_child(coro, turn: tuple[Path, str] | None):
    """Bind a turn-owned streaming worker, unlike unrelated background tasks."""
    if turn is None:
        return await coro
    token = scoped_turn_entry()
    try:
        bind_turn(*turn)
        return await coro
    finally:
        restore_turn(token)


@contextmanager
def transport_bypass():
    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def active_turn():
    turn = _CURRENT_TURN.get()
    if _BYPASS.get() or turn is None or turn[2] is not asyncio.current_task():
        return None
    return turn[:2]


def transport_id(event) -> str | None:
    update_id = getattr(event, "platform_update_id", None)
    if update_id is not None:
        return f"update:{update_id}"
    message_id = getattr(event, "message_id", None)
    if message_id is not None:
        return f"chat:{event.source.chat_id}:message:{message_id}"
    return getattr(event, "_outbox_transport_id", None)


def event_kind(event) -> str:
    return getattr(getattr(event, "message_type", None), "value", None) or "message"


def event_admission_scope(event, runner, home: Path) -> tuple:
    """Bind local replay to its native runner, routing identity and ingress payload."""
    source = event.source
    return (id(event), runner, Path(home).resolve(), str(source.profile or "default"),
            source.platform, source.chat_id, source.thread_id, source.user_id,
            transport_id(event), event_kind(event))


def local_admission_turn(event, runner, home: Path) -> str | None:
    """Only the same event's unchanged local admission can bypass transport dedup."""
    if (not getattr(event, "_outbox_duplicate", False)
            and getattr(event, "_outbox_admission_scope", None) == event_admission_scope(event, runner, home)):
        return getattr(event, "_outbox_turn_id", None)
    return None


def durable_control(method):
    """Control cards return a raw Telegram message so callback state can bind its ID."""
    @functools.wraps(method)
    async def wrapped(self, chat_id, text, *, parse_mode, thread_id, metadata,
                      reply_markup=None, reply_to_mode=None):
        config = getattr(getattr(self, "gateway_runner", None), "config", None)
        if not getattr(config, "durable_outbox_enabled", False) or active_turn() is None:
            return await method(self, chat_id, text, parse_mode=parse_mode,
                                thread_id=thread_id, metadata=metadata,
                                reply_markup=reply_markup, reply_to_mode=reply_to_mode)
        from gateway.platforms.base import SendResult
        payload = {
            "chat_id": chat_id, "text": text, "parse_mode": getattr(parse_mode, "value", parse_mode),
            "thread_id": thread_id, "metadata": metadata,
            "reply_markup": reply_markup.to_dict() if reply_markup is not None else None,
            "reply_to_mode": reply_to_mode,
        }
        message = None

        async def send(_payload):
            nonlocal message
            message = await method(self, chat_id, text, parse_mode=parse_mode,
                                   thread_id=thread_id, metadata=metadata,
                                   reply_markup=reply_markup, reply_to_mode=reply_to_mode)
            return SendResult(success=True, message_id=str(message.message_id))

        result = await deliver(self, "control_prompt", payload, send)
        if not result.success:
            raise RuntimeError(result.error or "control prompt outbox held")
        return message

    return wrapped


def durable_egress(kind: str):
    """Intercept an adapter's own egress only when opted in and inside a turn."""
    def decorate(method):
        signature = inspect.signature(method)

        @functools.wraps(method)
        async def wrapped(self, *args, **kwargs):
            config = getattr(getattr(self, "gateway_runner", None), "config", None)
            if not getattr(config, "durable_outbox_enabled", False) or active_turn() is None:
                return await method(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            payload = {k: v for k, v in bound.arguments.items() if k != "self" and k != "kwargs"}
            payload.update(bound.arguments.get("kwargs", {}))
            return await deliver(self, kind, payload, lambda p: method(self, **p))

        setattr(wrapped, "_durable_outbox", True)
        return wrapped
    return decorate


def _snapshot_file(home: Path, path: str) -> str:
    source = Path(path)
    target_dir = home / "gateway-outbox-media"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}-{source.name}"
    with source.open("rb") as inp, target.open("xb") as out:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
    descriptor = os.open(target_dir, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return str(target)


def _outbound_class_for_payload(payload: dict[str, Any]) -> str:
    """Persist the semantic class before a durable send crosses into a child task."""
    from gateway.platforms.base import OUTBOUND_FINAL, OUTBOUND_PROGRESS, current_outbound_class

    current = current_outbound_class()
    if current is not None:
        return current
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("_interim_send"):
        return OUTBOUND_PROGRESS
    return OUTBOUND_FINAL


def _replay_outbound_class(payload: dict[str, Any]) -> str:
    """Rebind replayed rows explicitly; legacy rows default to the protected final class."""
    from gateway.platforms.base import OUTBOUND_FINAL, OUTBOUND_NOTICE, OUTBOUND_PROGRESS

    kind = payload.get("_outbound_class")
    if kind in {OUTBOUND_FINAL, OUTBOUND_NOTICE, OUTBOUND_PROGRESS}:
        return kind
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("_interim_send"):
        return OUTBOUND_PROGRESS
    return OUTBOUND_FINAL


def wire_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"_outbox_original", "_outbound_class"}}


def _discard_delivered_media(home: Path, payload: dict[str, Any]) -> None:
    """Only delete copies owned by this outbox after the receipt commits."""
    if "_outbox_original" not in payload:
        return
    root = (home / "gateway-outbox-media").resolve()
    paths = [payload[k] for k in ("file_path", "image_path", "video_path", "audio_path") if k in payload]
    from urllib.parse import unquote
    paths.extend(unquote(url[7:]) for url, _ in payload.get("images", []) if url.startswith("file://"))
    for path in paths:
        candidate = Path(path).resolve()
        if candidate.parent == root:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to remove delivered outbox media %s", candidate, exc_info=True)


def _uncertain(result) -> bool:
    """Only a transport outcome without a definitive refusal can have been sent."""
    if (result.success or result.pre_send or result.retry_after is not None
            or result.raw_response is not None):
        return False
    from gateway.platforms.base import SEND_ERROR_KINDS
    kind = result.error_kind
    error = (result.error or "").lower()
    if _descriptor_exhaustion_text(error):
        return False  # No descriptor: the request's connection or file was never opened.
    if kind == "connectionrefused" or (
        kind in {"transient", "unknown", None}
        and ("connection refused" in error or "connectionrefused" in error)
    ):
        return False
    if kind == "transient":
        return True  # A connection may drop after the server accepts the send.
    if kind in SEND_ERROR_KINDS - {"unknown"}:
        return False
    return (any(marker in error for marker in
                ("timeout", "timed out", "network error", "connection reset",
                 "connection aborted", "server disconnected")) or
            (result.retryable and not any(marker in error for marker in
             ("not connected", "bad request", "too_long", "draft_rejected", "flood_control"))))


def _descriptor_exhaustion_text(error: str) -> bool:
    return "too many open files" in error or "[errno 24]" in error or "[errno 23]" in error


def _is_local_unsent_exception(exc: BaseException) -> bool:
    """A transport raised because it could not allocate a descriptor (EMFILE/ENFILE): opening the
    socket or file the request needed failed, so nothing reached the platform."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, OSError) and cur.errno in _DESCRIPTOR_ERRNOS:
            return True
        if _descriptor_exhaustion_text(str(cur).lower()):
            return True
        stack.extend(x for x in (cur.__cause__, cur.__context__) if x is not None)
    return False


def _is_transient_store_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and exc.errno in _DESCRIPTOR_ERRNOS:
        return True
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "unable to open database file" in text or "locked" in text or "disk i/o" in text
    return False


async def _store_io_retry(operation, *args, **kwargs):
    """``_store_io`` that waits out transient local store failures before giving up."""
    for delay in (*_STORE_RETRY_DELAYS, None):
        try:
            return await _store_io(operation, *args, **kwargs)
        except Exception as exc:
            if delay is None or not _is_transient_store_error(exc):
                raise
            await asyncio.sleep(delay)


async def _write_receipt(store: "Outbox", row: "OutboxRow", **kwargs) -> bool | None:
    """Record a send's known outcome; ``None`` when the store stayed unwritable (the row then
    stays held, never resent, and is reported once)."""
    try:
        return await _store_io_retry(store.receipt, row, **kwargs)
    except Exception as exc:
        if not _is_transient_store_error(exc):
            raise
        logger.error("Outbox receipt for %s could not be written (success=%s): %s; the row stays held",
                     row.idempotency_key, kwargs.get("success"), exc)
        return None


async def _store_io(operation, *args, **kwargs):
    """Finish a started disk operation even when its awaiting turn is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            logger.exception("Cancelled outbox disk operation failed")
        raise


async def open_outbox(home: Path) -> "Outbox":
    """Construct and migrate the store off the gateway event loop."""
    return await _store_io(Outbox, home)


def _prepare_row(store, home, turn_id, kind, payload):
    if store.held_payload(turn_id, kind, payload):
        return None
    previous = store.pending_retry(turn_id, kind, payload)
    if previous is not None:
        return previous
    durable = dict(payload)
    original_media = False
    for key in ("file_path", "image_path", "video_path", "audio_path"):
        if key in durable:
            durable[key] = _snapshot_file(home, durable[key])
            original_media = True
    if "images" in durable:
        from urllib.parse import quote, unquote
        images = []
        for url, alt in durable["images"]:
            if url.startswith("file://"):
                url = "file://" + quote(_snapshot_file(home, unquote(url[7:])))
                original_media = True
            images.append((url, alt))
        durable["images"] = images
    if original_media:
        durable["_outbox_original"] = payload
    return store.enqueue(turn_id, kind, durable)


async def deliver(adapter, kind: str, payload: dict[str, Any], send):
    """Commit an ordered row before crossing the transport boundary.

    ``send`` is the adapter's original send/edit/media coroutine. Any uncertain
    transport failure stays held; callers cannot silently turn it into a resend.
    """
    from gateway.platforms.base import SendResult

    turn = active_turn()
    if turn is None:
        return await send(payload)
    payload = dict(payload)
    payload.setdefault("_outbound_class", _outbound_class_for_payload(payload))
    home, turn_id = turn
    store = await _store_io_retry(store_for, home)
    lock = _TURN_LOCKS.setdefault(turn, asyncio.Lock())
    async with lock:
        # Keep the lookup, snapshot and enqueue ordered before transport dispatch.
        try:
            row = await _store_io_retry(_prepare_row, store, home, turn_id, kind, payload)
        except (TypeError, ValueError) as exc:
            logger.error("Outbox could not serialize %s; refusing unreceipted transport: %s", kind, exc)
            return SendResult(success=False, error="outbox payload could not be serialized")
        if row is None:
            return SendResult(success=False, error="outbox payload has an uncertain prior dispatch", held=True)
        if not await _store_io_retry(store.begin_send, row):
            return SendResult(success=False, error="earlier outbox row is unresolved", held=True)
        flight = (store.path, row.idempotency_key)
        _IN_FLIGHT.add(flight)
        try:
            try:
                with transport_bypass():
                    result = await send(wire_payload(row.payload))
            except BaseException as exc:
                await _write_receipt(store, row, message_id=None, success=False,
                                     uncertain=not _is_local_unsent_exception(exc))
                raise
            retry_after = (result.retry_after if kind == "send" and
                           not (isinstance(payload.get("metadata"), dict) and
                                payload["metadata"].get("_interim_send")) else None)
            scheduled = await _write_receipt(
                store, row, message_id=str(result.message_id) if result.message_id else None,
                success=bool(result.success), uncertain=_uncertain(result), retry_after=retry_after)
        finally:
            _IN_FLIGHT.discard(flight)
        if scheduled is None:
            return result
        if scheduled:
            await _schedule_retry(store, adapter)
            # The send is accepted for durable redelivery. Consumers must not
            # launch an independent fallback while this row is waiting.
            return SendResult(success=True, deferred=True, retry_after=retry_after)
        if result.success:
            await _store_io(_discard_delivered_media, home, row.payload)
        return result


def _retry_profile(adapter) -> str | None:
    runner = getattr(adapter, "gateway_runner", None)
    for profile, adapters in (getattr(runner, "_profile_adapters", None) or {}).items():
        if adapters.get(adapter.platform) is adapter:
            return profile
    return None


def _current_retry_adapter(adapter, profile: str | None):
    runner = getattr(adapter, "gateway_runner", None)
    if profile is not None:
        return (getattr(runner, "_profile_adapters", None) or {}).get(profile, {}).get(adapter.platform)
    adapters = getattr(runner, "adapters", None)
    if adapters is None:
        return adapter  # Standalone adapter (including tests) has no reconnect registry.
    return adapters.get(adapter.platform)


async def _schedule_retry(store: "Outbox", adapter) -> None:
    """Ensure one coalesced deferred sweep for this store and adapter identity, without blocking
    ingress. A sweep that is already parked is woken to re-read the earliest deadline."""
    profile = _retry_profile(adapter)
    key = (store.path, getattr(adapter, "platform", None), profile)
    existing = _SWEEPS.get(key)
    if existing is not None and not existing[0].done():
        existing[1].set()
        return
    wake = asyncio.Event()
    from gateway.platforms.base import OUTBOUND_FINAL, outbound_class
    # Background sweeps must never inherit a caller's notice/progress label. Each row
    # is rebound below from its persisted class before its adapter call.
    with outbound_class(OUTBOUND_FINAL):
        task = asyncio.create_task(_deferred_sweep(store, adapter, profile, key, wake))
    _SWEEPS[key] = (task, wake)


async def _deferred_sweep(store: "Outbox", adapter, profile: str | None, key: tuple,
                          wake: asyncio.Event) -> None:
    """Sleep to the store's earliest retry deadline, run one serialized recover(), repeat until
    no deferred row remains. A failed pass backs off instead of ending the sweep or spinning."""
    failures = 0
    try:
        while True:
            wake.clear()
            try:
                deadline = await _store_io_retry(store.next_retry_at)
            except Exception:
                deadline = time.time() + _SWEEP_ERROR_BACKOFF_SECS[min(failures, len(_SWEEP_ERROR_BACKOFF_SECS) - 1)]
                failures += 1
                logger.warning("Outbox sweep could not read retry deadlines for %s", store.path, exc_info=True)
            if deadline is None:
                return
            delay = deadline - time.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                    continue  # An earlier deadline was scheduled; re-read it.
                except TimeoutError:
                    pass
            current = _current_retry_adapter(adapter, profile)
            if current is None:
                return  # No live adapter for this identity; the next boot or schedule resumes.
            started = time.monotonic()
            try:
                await recover(store, current, startup=False)
                failures = 0
            except Exception:
                backoff = _SWEEP_ERROR_BACKOFF_SECS[min(failures, len(_SWEEP_ERROR_BACKOFF_SECS) - 1)]
                failures += 1
                logger.warning("Outbox deferred sweep failed for %s; retrying in %.0fs", store.path, backoff,
                               exc_info=True)
                await asyncio.sleep(backoff)
                continue
            # A due row that this pass could not claim must not turn the sweep into a busy loop.
            elapsed = time.monotonic() - started
            if elapsed < _SWEEP_IDLE_FLOOR_SECS:
                await asyncio.sleep(_SWEEP_IDLE_FLOOR_SECS - elapsed)
    finally:
        entry = _SWEEPS.get(key)
        if entry is not None and entry[0] is asyncio.current_task():
            _SWEEPS.pop(key, None)


def _recover_lock(store: "Outbox") -> asyncio.Lock:
    locks = _RECOVER_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(store.path, asyncio.Lock())


def _report_held(store: "Outbox", ambiguous: list["OutboxRow"]) -> int:
    """Log each held row once per process; rows this process is dispatching are in flight."""
    held = [row for row in ambiguous if (store.path, row.idempotency_key) not in _IN_FLIGHT]
    keys = {(store.path, row.idempotency_key) for row in held}
    for row in held:
        if (store.path, row.idempotency_key) not in _HELD_LOGGED:
            logger.error("Held ambiguous outbox dispatch: turn=%s sequence=%s key=%s",
                         row.turn_id, row.sequence, row.idempotency_key)
    _HELD_LOGGED.difference_update({k for k in _HELD_LOGGED if k[0] == store.path} - keys)
    _HELD_LOGGED.update(keys)
    return len(held)


def _retention_days(home: Path) -> int:
    """Read only this setting using the gateway loader's layer precedence."""
    from gateway import config_loader
    from gateway.config import GatewayConfig, validate_outbox_retention_days
    import hermes_yaml as yaml

    default = GatewayConfig.durable_outbox_retention_days
    legacy = config_loader.load_legacy_gateway_json(home)
    try:
        yaml_cfg = config_loader.read_yaml_layers(home)
        gateway = yaml_cfg.get("gateway")
        found, outbox = config_loader._bridge_lookup(yaml_cfg, gateway, legacy,
                                                       "durable_outbox", "presence")
        if not found:
            outbox = legacy.get("durable_outbox", {})
        if outbox is None:
            outbox = {}
        if not isinstance(outbox, dict):
            raise ValueError("gateway.durable_outbox must be a mapping")
        return validate_outbox_retention_days(outbox.get("retention_days", default))
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        logger.warning("Invalid outbox retention for %s; using %s days: %s", home, default, exc)
        return default


async def recover(store: "Outbox", adapter, *, startup: bool = True) -> tuple[int, int]:
    """Replay proven-unsent rows; only boot replays fresh unclaimed rows. Serialized per store."""
    async with _recover_lock(store):
        sent, held = await _recover_locked(store, adapter, startup=startup)
    await _schedule_retry(store, adapter)
    return sent, held


async def _recover_locked(store: "Outbox", adapter, *, startup: bool) -> tuple[int, int]:
    sent = 0
    while True:
        pending = await _store_io(store.pending, startup=startup)
        if not pending:
            break
        advanced = False
        for row in pending:
            if row.type not in {"send", "edit_message", "send_document", "send_image_file",
                                "send_video", "send_voice", "send_multiple_images", "send_image",
                                "send_animation", "control_prompt"}:
                logger.error("Unsupported pending outbox type %s for %s", row.type, row.idempotency_key)
                continue
            if not await _store_io_retry(store.begin_send, row):
                continue
            flight = (store.path, row.idempotency_key)
            _IN_FLIGHT.add(flight)
            try:
                from gateway.platforms.base import outbound_class
                with outbound_class(_replay_outbound_class(row.payload)):
                    with transport_bypass():
                        if row.type == "control_prompt":
                            from gateway.platforms.base import SendResult
                            from telegram import InlineKeyboardMarkup
                            payload = wire_payload(row.payload)
                            markup = payload.get("reply_markup")
                            if markup is not None:
                                payload["reply_markup"] = InlineKeyboardMarkup.de_json(markup, adapter._bot)
                            message = await adapter._send_control_message(**payload)
                            result = SendResult(success=True, message_id=str(message.message_id))
                        else:
                            payload = wire_payload(row.payload)
                            if row.type == "send_multiple_images" and "images" in payload:
                                payload["images"] = [tuple(image) for image in payload["images"]]
                            result = await getattr(adapter, row.type)(**payload)
            except asyncio.CancelledError:
                await _write_receipt(store, row, message_id=None, success=False)
                _IN_FLIGHT.discard(flight)
                raise
            except Exception as exc:
                local = _is_local_unsent_exception(exc)
                await _write_receipt(store, row, message_id=None, success=False, uncertain=not local)
                _IN_FLIGHT.discard(flight)
                if local:
                    logger.warning("Outbox dispatch %s refused locally (%s); not sent", row.idempotency_key, exc)
                else:
                    logger.exception("Ambiguous outbox dispatch %s", row.idempotency_key)
                continue
            retry_after = (result.retry_after if row.type == "send" and
                           not (isinstance(row.payload.get("metadata"), dict) and
                                row.payload["metadata"].get("_interim_send")) else None)
            try:
                await _write_receipt(
                    store, row, message_id=str(result.message_id) if result.message_id else None,
                    success=bool(result.success), uncertain=_uncertain(result), retry_after=retry_after)
            finally:
                _IN_FLIGHT.discard(flight)
            if result.success:
                await _store_io(_discard_delivered_media, store.path.parent, row.payload)
                sent += 1
                advanced = True
        if not advanced:
            break
    held = _report_held(store, await _store_io_retry(store.ambiguous))
    if startup:
        retention = await _store_io(_retention_days, store.path.parent)
        await _store_io(store.prune, retention_days=retention)
    return sent, held


@dataclass(frozen=True)
class OutboxRow:
    turn_id: str
    sequence: int
    type: str
    payload: dict[str, Any]
    idempotency_key: str
    owner_epoch: int
    state: str
    message_id: str | None


class Outbox:
    def __init__(self, home: Path):
        self.path = Path(home) / "gateway-outbox.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS admissions (
                    profile TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    transport_event_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    result TEXT,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    PRIMARY KEY (profile, platform, transport_event_id, event_kind)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    turn_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    owner_epoch INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'sending', 'ambiguous', 'delivered', 'failed_unsent', 'expired_ambiguous')),
                    message_id TEXT,
                    send_status TEXT,
                    edit_status TEXT,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    retry_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (turn_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state, turn_id, sequence);
            """)
            db.execute("BEGIN IMMEDIATE")
            try:
                if not any(col[1] == "created_at" for col in db.execute("PRAGMA table_info(admissions)")):
                    db.execute("ALTER TABLE admissions ADD COLUMN created_at REAL")
                    db.execute("UPDATE admissions SET created_at=? WHERE created_at IS NULL", (time.time(),))
                schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone()[0]
                cols = {col[1] for col in db.execute("PRAGMA table_info(outbox)")}
                if (not {"created_at", "retry_at", "attempts"} <= cols
                        or "expired_ambiguous" not in schema or "failed_unsent" not in schema):
                    db.execute("""CREATE TABLE outbox_migrated (
                        turn_id TEXT NOT NULL, sequence INTEGER NOT NULL, type TEXT NOT NULL,
                        payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                        owner_epoch INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','sending','ambiguous','delivered','failed_unsent','expired_ambiguous')),
                        message_id TEXT, send_status TEXT, edit_status TEXT,
                        created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                        retry_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (turn_id, sequence))""")
                    fields = ("turn_id", "sequence", "type", "payload", "idempotency_key", "owner_epoch",
                              "state", "message_id", "send_status", "edit_status", "created_at", "retry_at", "attempts")
                    defaults = {"created_at": "strftime('%s','now')", "retry_at": "NULL", "attempts": "0"}
                    selected = ", ".join(field if field in cols else defaults[field] for field in fields)
                    db.execute(f"INSERT INTO outbox_migrated SELECT {selected} FROM outbox")
                    db.execute("DROP TABLE outbox")
                    db.execute("ALTER TABLE outbox_migrated RENAME TO outbox")
                    db.execute("CREATE INDEX outbox_state ON outbox(state, turn_id, sequence)")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def _db(self):
        """One connection per operation, always closed: ``with sqlite3.Connection`` only commits,
        and each unclosed connection held its db/WAL/SHM descriptors until GC (#69567)."""
        with closing(self._connect()) as db:
            yield db

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        # SQLite's journal-mode switch may report BUSY immediately even with a
        # busy_timeout when two openers switch the same legacy DB to WAL.
        for attempt in range(100):
            try:
                db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 99:
                    db.close()
                    raise
                time.sleep(0.05)
        db.row_factory = sqlite3.Row
        return db

    def lookup(self, profile: str, platform: str, transport_event_id: str,
               event_kind: str) -> tuple[str, str | None] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT turn_id, result FROM admissions WHERE profile=? AND platform=? "
                "AND transport_event_id=? AND event_kind=?",
                (profile, platform, transport_event_id, event_kind),
            ).fetchone()
            return (row[0], row[1]) if row else None

    def admit(self, profile: str, platform: str, transport_event_id: str | None,
              event_kind: str, *, fallback_id: str | None = None) -> tuple[str, bool]:
        """Return (original turn ID, newly admitted). Synthetic IDs must be durable upstream."""
        event_id = transport_event_id or fallback_id or uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                turn_id = uuid.uuid4().hex
                inserted = db.execute(
                    "INSERT OR IGNORE INTO admissions (profile, platform, transport_event_id, event_kind, turn_id, result, created_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (profile, platform, str(event_id), event_kind, turn_id, time.time()),
                ).rowcount
                if not inserted:
                    turn_id = db.execute(
                        "SELECT turn_id FROM admissions WHERE profile=? AND platform=? "
                        "AND transport_event_id=? AND event_kind=?",
                        (profile, platform, str(event_id), event_kind),
                    ).fetchone()[0]
                db.commit()
                return turn_id, bool(inserted)
            except BaseException:
                db.rollback()
                raise

    def finish_admission(self, turn_id: str, result: str | None) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE admissions SET result=? WHERE turn_id=?", (result, turn_id))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def original_result(self, turn_id: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT result FROM admissions WHERE turn_id=?", (turn_id,)).fetchone()
            return row[0] if row else None

    def pending_retry(self, turn_id: str, kind: str, payload: dict[str, Any]) -> OutboxRow | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM outbox WHERE turn_id=? ORDER BY sequence DESC LIMIT 1",
                             (turn_id,)).fetchone()
            if row and row["state"] == "pending" and row["retry_at"] is None and row["type"] == kind:
                stored = json.loads(row["payload"])
                if stored.get("_outbox_original", stored) == payload:
                    return self._row(row)
            return None

    def held_payload(self, turn_id: str, kind: str, payload: dict[str, Any]) -> bool:
        with self._db() as db:
            for row in db.execute("SELECT payload FROM outbox WHERE turn_id=? AND type=? "
                                  "AND (state IN ('sending','ambiguous','expired_ambiguous') "
                                  "OR (state='pending' AND retry_at IS NOT NULL))", (turn_id, kind)):
                stored = json.loads(row[0])
                if stored.get("_outbox_original", stored) == payload:
                    return True
        return False

    def enqueue(self, turn_id: str, kind: str, payload: dict[str, Any], owner_epoch: int = 0,
                idempotency_key: str | None = None) -> OutboxRow:
        key = idempotency_key or uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT * FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
                if existing:
                    db.commit()
                    return self._row(existing)
                seq = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM outbox WHERE turn_id=?",
                                 (turn_id,)).fetchone()[0]
                db.execute("INSERT INTO outbox (turn_id, sequence, type, payload, idempotency_key, owner_epoch) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (turn_id, seq, kind, json.dumps(payload, default=str), key, owner_epoch))
                db.commit()
                return OutboxRow(turn_id, seq, kind, payload, key, owner_epoch, "pending", None)
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _row(row: sqlite3.Row) -> OutboxRow:
        return OutboxRow(row["turn_id"], row["sequence"], row["type"], json.loads(row["payload"]),
                         row["idempotency_key"], row["owner_epoch"], row["state"], row["message_id"])

    def pending(self, *, startup: bool = True) -> list[OutboxRow]:
        with self._db() as db:
            if startup:
                return [self._row(r) for r in db.execute(
                    "SELECT * FROM outbox WHERE state='pending' AND (retry_at IS NULL OR retry_at<=?) "
                    "ORDER BY rowid", (time.time(),))]
            return [self._row(r) for r in db.execute(
                "SELECT * FROM outbox WHERE state='pending' AND retry_at<=? ORDER BY rowid",
                (time.time(),))]

    def scheduled(self) -> list[tuple[OutboxRow, float]]:
        with self._db() as db:
            return [(self._row(r), r["retry_at"]) for r in db.execute(
                "SELECT * FROM outbox WHERE state='pending' AND retry_at IS NOT NULL ORDER BY retry_at")]

    def next_retry_at(self) -> float | None:
        with self._db() as db:
            return db.execute("SELECT MIN(retry_at) FROM outbox WHERE state='pending' "
                              "AND retry_at IS NOT NULL").fetchone()[0]

    def all_rows(self) -> list[OutboxRow]:
        with self._db() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM outbox ORDER BY rowid")]

    def ambiguous(self) -> list[OutboxRow]:
        with self._db() as db:
            expired = db.execute("UPDATE outbox SET state='expired_ambiguous' "
                                 "WHERE state IN ('sending','ambiguous') AND created_at<?",
                                 (time.time() - 86400,)).rowcount
            if expired:
                logger.error("Expired %s uncertain outbox sends without replay; inspect %s",
                             expired, self.path)
            return [self._row(r) for r in db.execute(
                "SELECT * FROM outbox WHERE state IN ('sending','ambiguous') ORDER BY rowid")]

    def prune(self, *, retention_days: int = 7) -> None:
        """Keep uncertain work temporarily, bounding even unresolved history at 4x retention."""
        cutoff = time.time() - retention_days * 86400
        hard_cutoff = time.time() - 4 * retention_days * 86400
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                terminal = db.execute(
                    "SELECT payload FROM outbox WHERE (state IN ('delivered','failed_unsent') "
                    "AND created_at<?) OR (state='expired_ambiguous' AND created_at<?)",
                    (cutoff, hard_cutoff)).fetchall()
                db.execute("DELETE FROM outbox WHERE state IN ('delivered','failed_unsent') "
                           "AND created_at<?", (cutoff,))
                db.execute("DELETE FROM outbox WHERE state='expired_ambiguous' AND created_at<?", (hard_cutoff,))
                db.execute("DELETE FROM admissions WHERE created_at<? AND result IS NOT NULL "
                           "AND NOT EXISTS (SELECT 1 FROM outbox WHERE outbox.turn_id=admissions.turn_id "
                           "AND outbox.state NOT IN ('delivered','failed_unsent'))", (cutoff,))
                db.execute("DELETE FROM admissions WHERE created_at<? AND result IS NULL "
                           "AND NOT EXISTS (SELECT 1 FROM outbox WHERE outbox.turn_id=admissions.turn_id)",
                           (hard_cutoff,))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        for row in terminal:
            _discard_delivered_media(self.path.parent, json.loads(row[0]))

    def status(self) -> list[dict[str, Any]]:
        self.ambiguous()  # expire old holds and log them
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT turn_id, sequence, type, idempotency_key, state, created_at, "
                "retry_at, attempts, send_status, edit_status FROM outbox "
                # Legacy databases may still contain synthetic continuation rows.
                "WHERE state IN ('sending','ambiguous','expired_ambiguous','pending','failed_unsent') "
                "AND COALESCE(send_status, '') != 'synthetic' "
                "ORDER BY created_at DESC")]

    def begin_send(self, row: OutboxRow) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute("UPDATE outbox SET state='sending', retry_at=NULL, attempts=attempts+1 "
                                     "WHERE turn_id=? AND sequence=? AND state='pending' "
                                     "AND (retry_at IS NULL OR retry_at<=?)",
                                     (row.turn_id, row.sequence, time.time())).rowcount
                db.commit()
                return bool(changed)
            except BaseException:
                db.rollback()
                raise

    def receipt(self, row: OutboxRow, *, message_id: str | None, success: bool,
                uncertain: bool = True, retry_after: float | None = None) -> bool:
        """Return whether one server-directed, proven-unsent retry was queued."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                attempts = db.execute("SELECT attempts FROM outbox WHERE turn_id=? AND sequence=?",
                                      (row.turn_id, row.sequence)).fetchone()[0]
                scheduled = bool(not success and retry_after is not None and attempts == 1)
                state = ("delivered" if success else "pending" if scheduled else
                         "ambiguous" if uncertain else "failed_unsent")
                column = "edit_status" if row.type == "edit_message" else "send_status"
                db.execute(f"UPDATE outbox SET state=?, message_id=?, {column}=?, retry_at=? "
                           "WHERE turn_id=? AND sequence=? AND state='sending'",
                           (state, message_id, "success" if success else state,
                            time.time() + min(max(0, retry_after or 0), 86400) if scheduled else None,
                            row.turn_id, row.sequence))
                db.commit()
                return scheduled
            except BaseException:
                db.rollback()
                raise


if __name__ == "__main__":
    import argparse
    from hermes_constants import get_hermes_home

    parser = argparse.ArgumentParser(description="Inspect durable outbox unresolved deliveries")
    parser.add_argument("--home", type=Path, default=get_hermes_home())
    args = parser.parse_args()
    print(json.dumps(Outbox(args.home).status(), indent=2))
