"""Content-free observations of resident session state, never a transcript loader."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace
from typing import Any


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _message_shape(message: dict) -> dict:
    """Compare replay content without DB-only row identities or timestamps."""
    shape = {
        key: message[key]
        for key in ("role", "content", "tool_calls", "tool_call_id")
        if key in message
    }
    name = message.get("name", message.get("tool_name"))
    if name is not None:
        shape["name"] = name
    return shape


def _recall_calls(
    messages: list[dict], start: int, end: int
) -> dict[str, tuple[int, str]] | None:
    calls: dict[str, tuple[int, str]] = {}
    all_ids: set[str] = set()
    for i in range(start, end):
        row = messages[i]
        if row.get("role") != "assistant":
            continue
        for call in row.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if call_id in all_ids:
                return None
            all_ids.add(call_id)
            function = call.get("function")
            if (
                not isinstance(function, dict)
                or function.get("name") != "hindsight_recall"
            ):
                continue
            try:
                args = json.loads(function["arguments"])
            except (KeyError, TypeError, ValueError):
                continue
            query = args.get("query") if isinstance(args, dict) else None
            if isinstance(query, str) and query.strip():
                calls[call_id] = (i, query)
    return calls


def recall_witness(
    messages: list[dict],
    *,
    boundary: list[dict] | None,
    saved: list[dict] | None,
    session_id: str,
    provider: Any,
    probe: dict,
    private: bool,
    busy: bool,
) -> dict:
    """Witness a dispatch-bound native recall return, not a prompt or an audit row.

    ``None`` is an unknown boundary, not an empty prefix. The provider must be the
    resident session's Hindsight provider. Its explicit flag is checked rather
    than letting a diagnostic incognito ContextVar manufacture read-only evidence.
    """
    marker, expected = probe["marker"], probe["expected_fact"]
    result = {
        "source": "private_tool_return",
        "tool_name": "hindsight_recall",
        "marker_sha256": _digest(marker),
        "matched": False,
        "private_boundary": False,
        "provider_read_only": False,
        "result_nonempty": False,
        "expected_fact_match": False,
        "call_id_sha256": None,
        "query_sha256": None,
        "result_text_sha256": None,
    }
    if not private or not session_id:
        return result
    result["provider_read_only"] = bool(
        provider is not None
        and getattr(provider, "name", None) == "hindsight"
        and getattr(provider, "_session_id", None) == session_id
        and getattr(provider, "_read_only", False) is True
    )
    if (
        not isinstance(boundary, list)
        or not isinstance(saved, list)
        or len(messages) < len(boundary)
        or not all(isinstance(row, dict) for row in [*messages, *boundary, *saved])
        or [_message_shape(row) for row in messages[: len(boundary)]]
        != [_message_shape(row) for row in boundary]
        or marker in _json(boundary)
        or marker in _json(saved)
    ):
        return result
    result["private_boundary"] = True
    epoch = getattr(provider, "_privacy_epoch", None)
    bank = getattr(provider, "_bank_id", None)
    if (
        busy
        or not result["provider_read_only"]
        or type(epoch) is not int
        or not isinstance(bank, str)
        or not bank
    ):
        return result
    marked = [
        i
        for i, row in enumerate(messages)
        if i >= len(boundary)
        and row.get("role") == "user"
        and marker in _text(row.get("content"))
    ]
    if len(marked) != 1:
        return result
    start = marked[0] + 1
    end = next(
        (i for i in range(start, len(messages)) if messages[i].get("role") == "user"),
        len(messages),
    )
    calls = _recall_calls(messages, start, end)
    if calls is None:
        return result
    candidates = []
    returned: set[str] = set()
    for i in range(start, end):
        row = messages[i]
        if row.get("role") != "tool":
            continue
        call_id = row.get("tool_call_id")
        if not isinstance(call_id, str) or call_id not in calls:
            continue
        if call_id in returned:
            return result
        returned.add(call_id)
        call_index, query = calls[call_id]
        if i <= call_index or any(
            row.get(key) not in (None, "hindsight_recall")
            for key in ("name", "tool_name")
        ):
            continue
        try:
            payload = json.loads(row["content"])
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or "error" in payload:
            continue
        text, proof = payload.get("result"), payload.get("_hermes_private_read")
        if not isinstance(text, str) or not text.strip() or not isinstance(proof, dict):
            continue
        if not (
            proof.get("no_store") is True
            and proof.get("result_nonempty") is True
            and type(proof.get("epoch")) is int
            and proof["epoch"] == epoch
            and proof.get("session_sha256") == _digest(session_id)
            and proof.get("bank_sha256") == _digest(bank)
            and proof.get("query_sha256") == _digest(query)
            and proof.get("result_text_sha256") == _digest(text)
        ):
            continue
        candidates.append(
            {
                "matched": True,
                "result_nonempty": True,
                "expected_fact_match": bool(expected.strip()) and expected in text,
                "call_id_sha256": _digest(call_id),
                "query_sha256": _digest(query),
                "result_text_sha256": _digest(text),
            }
        )
    if candidates:
        result.update(
            next(
                (row for row in candidates if row["expected_fact_match"]),
                candidates[-1],
            )
        )
    return result


def _resident_db(store, session_key):
    from gateway.session_persistence import _DB_UNPINNED

    pinned = store._db_pinned
    if pinned is not _DB_UNPINNED:
        return pinned
    profile = store._named_profile_for_key(session_key)
    home = (
        store._routing_home
        if profile in (None, "default")
        else store._profile_home_cache.get(profile)
    )
    if home is None or not store._db_handles_lock.acquire(blocking=False):
        raise RuntimeError("Session snapshot unavailable")
    try:
        return store._db_handles.get(home / "state.db")
    finally:
        store._db_handles_lock.release()


def _private_store_count(session_key, session_id):
    from agent import incognito as privacy
    from gateway import incognito, outbox

    # Each lock is held alone, briefly and non-blocking. An observer must not
    # contend with an ingress transition or initialize a namespace on a miss.
    if not privacy._lock.acquire(blocking=False):
        raise RuntimeError("Session snapshot unavailable")
    try:
        registered = tuple(
            key in privacy._sessions for key in (session_key, session_id)
        )
        count = sum(
            len(privacy._private_states.get(key, {}))
            for key in (session_key, session_id)
        )
    finally:
        privacy._lock.release()
    if not incognito._RAM_LOCK.acquire(blocking=False):
        raise RuntimeError("Session snapshot unavailable")
    try:
        count += len(incognito._RAM_TOPICS.get(session_key, ()))
    finally:
        incognito._RAM_LOCK.release()
    if not outbox._STORES_LOCK.acquire(blocking=False):
        raise RuntimeError("Session snapshot unavailable")
    try:
        suffix = ":incognito:" + session_key
        count += sum(str(key).endswith(suffix) for key in outbox._STORES)
    finally:
        outbox._STORES_LOCK.release()
    return count, registered


def _loop_view(runner, session_key):
    """Peek loop-owned state. No loader, resolver, factory or blocking lock."""
    from gateway.incognito import transition_pending
    from gateway.run import _AGENT_PENDING_SENTINEL

    store = runner.session_store
    if transition_pending(session_key) or not store._lock.acquire(blocking=False):
        raise RuntimeError("Session snapshot unavailable")
    try:
        if not store._loaded:
            raise RuntimeError("Session snapshot unavailable")
        entry = store._entries.get(session_key)
        session_id = entry.session_id if entry is not None else None
        private = entry.incognito is True if entry is not None else False
        record = getattr(store, "_incognito_records", {}).get(session_key)
        if private != isinstance(record, dict) or (
            isinstance(record, dict) and record.get("session_id") != session_id
        ):
            raise RuntimeError("Session snapshot unavailable")
        history = getattr(store, "_incognito_histories", {}).get(session_id)
        if (private and not isinstance(history, list)) or (
            history is not None and not isinstance(history, list)
        ):
            raise RuntimeError("Session snapshot unavailable")
        profile = store._named_profile_for_key(session_key) if entry is not None else None
    finally:
        store._lock.release()
    db = _resident_db(store, session_key) if entry is not None else None
    if entry is not None and db is None:
        raise RuntimeError("Session snapshot unavailable")
    state = runner.__dict__.get("_sessions", {}).get(session_key)
    running = state.turn.agent if state is not None else None
    busy = running is not None
    agent = running if running is not _AGENT_PENDING_SENTINEL else None
    route_source = "resident" if agent is not None else "none"
    cache_lock = runner._agent_cache_lock
    if not cache_lock.acquire(blocking=False):
        raise RuntimeError("Session snapshot unavailable")
    try:
        cached = runner._agent_cache.get(session_key)
        if agent is None and cached is not None:
            agent = cached[0]
            route_source = "cached"
    finally:
        cache_lock.release()
    if agent is not None and getattr(agent, "session_id", None) != session_id:
        raise RuntimeError("Session snapshot unavailable")
    messages = getattr(agent, "_session_messages", None) if agent is not None else None
    if not isinstance(messages, list):
        messages = history if isinstance(history, list) else None
    if agent is not None and messages is None:
        raise RuntimeError("Session snapshot unavailable")
    snapshot = (
        getattr(agent, "_incognito_snapshot", None) if agent is not None else None
    )
    boundary = snapshot.get("_session_messages") if isinstance(snapshot, dict) else None
    manager = getattr(agent, "_memory_manager", None) if agent is not None else None
    provider = getattr(manager, "_tool_to_provider", {}).get("hindsight_recall")
    if provider is not None and getattr(manager, "_session_id", None) != session_id:
        raise RuntimeError("Session snapshot unavailable")
    provider_state = SimpleNamespace(
        **{
            name: getattr(provider, name, None)
            for name in (
                "name",
                "_session_id",
                "_read_only",
                "_privacy_epoch",
                "_bank_id",
            )
        }
    )
    parked = getattr(runner, "_incognito_parked_agents", {}).get(session_key)
    parked_agent = parked[0] if parked is not None else None
    held_snapshots = getattr(runner, "_incognito_gateway_snapshots", {})
    agents = {id(item): item for item in (agent, parked_agent) if item is not None}
    snapshot_count = int(session_key in held_snapshots) + sum(
        isinstance(getattr(item, "_incognito_snapshot", None), dict)
        for item in agents.values()
    )
    private_stores, registered = _private_store_count(session_key, session_id)
    if private != all(registered) or (not private and any(registered)):
        raise RuntimeError("Session snapshot unavailable")
    model = getattr(agent, "model", None) if agent is not None else None
    route_provider = getattr(agent, "provider", None) if agent is not None else None
    if any(
        value is not None and not isinstance(value, str)
        for value in (model, route_provider)
    ):
        raise RuntimeError("Session snapshot unavailable")
    if agent is not None and not hasattr(agent, "_cached_system_prompt"):
        raise RuntimeError("Session snapshot unavailable")
    prompt = (
        getattr(agent, "_cached_system_prompt", None) if agent is not None else None
    )
    generation = (
        (state.persistent.run_generation, state.persistent.model_override_epoch)
        if state is not None
        else None
    )
    token = (
        id(store),
        id(entry),
        session_id,
        profile,
        private,
        id(record),
        id(db),
        id(state),
        generation,
        busy,
        id(agent),
        model,
        route_provider,
        id(manager),
        getattr(manager, "_session_id", None),
        id(provider),
        tuple(vars(provider_state).values()),
        id(history),
        len(history) if history is not None else None,
        id(messages),
        len(messages) if messages is not None else None,
        id(boundary),
        id(prompt),
        snapshot_count,
        private_stores,
        id(held_snapshots.get(session_key)),
        id(parked),
    )
    return {
        "found": entry is not None,
        "session_id": session_id,
        "incognito": private,
        "busy": busy,
        "agent_present": agent is not None,
        "model": model,
        "provider": route_provider,
        "route_source": route_source,
        "history": history,
        "agent_history": messages,
        "boundary": boundary,
        "prompt": prompt,
        "memory_provider": provider_state,
        "db": db,
        "snapshot_count": snapshot_count,
        "parked_agent_count": int(parked is not None),
        "private_store_count": private_stores,
        "token": token,
        # Keep observed owners alive so a replacement cannot reuse an old object
        # address between views. This fence lives only for this socket request.
        "owners": (
            entry,
            record,
            db,
            state,
            agent,
            manager,
            provider,
            parked,
            held_snapshots.get(session_key),
        ),
    }


def make_session_snapshot_handler(runner, main_loop):
    """Inspect resident state in the socket worker, fencing loop-owned generations.

    No raw content escapes, including through errors. A busy or changed capture
    never provides positive recall evidence. Calls made on the loop itself fail
    immediately instead of waiting on their own loop or doing SQLite I/O there.
    """
    loop_thread = threading.get_ident()

    async def view(session_key):
        return _loop_view(runner, session_key)

    def observe(session_key):
        future = asyncio.run_coroutine_threadsafe(view(session_key), main_loop)
        try:
            return future.result(timeout=2.0)
        finally:
            future.cancel()

    def handler(params):
        from agent.incognito import incognito_scope

        try:
            if threading.get_ident() == loop_thread or not isinstance(params, dict):
                raise RuntimeError("Session snapshot unavailable")
            key, probe = params.get("session_key"), params.get("recall_probe")
            if not isinstance(key, str) or not key or len(key) > 4096:
                raise RuntimeError("Session snapshot unavailable")
            if probe is not None and (
                not isinstance(probe, dict)
                or any(
                    not isinstance(probe.get(name), str) or not probe[name].strip()
                    for name in ("marker", "expected_fact")
                )
            ):
                raise RuntimeError("Session snapshot unavailable")
            # An unbound scope suppresses accidental diagnostics without creating
            # a private state bucket for the observed session or changing its flag.
            with incognito_scope(True, ""):
                first = observe(key)
                saved = (
                    first["db"].get_messages_as_conversation(
                        first["session_id"],
                        repair_alternation=True,
                        resident_only=True,
                    )
                    if first["found"]
                    else []
                )
                history = deepcopy(
                    first["history"] if isinstance(first["history"], list) else saved
                )
                messages = deepcopy(first["agent_history"])
                if messages is None:
                    messages = []
                boundary = deepcopy(first["boundary"])
                # A newly built private-route agent starts with an empty agent
                # snapshot but replays the known saved prefix into its RAM history.
                if (
                    first["incognito"]
                    and isinstance(boundary, list)
                    and not boundary
                    and saved
                ):
                    boundary = saved
                result = {
                    name: first[name]
                    for name in ("found", "session_id", "incognito", "busy")
                }
                result.update(
                    history={
                        "count": len(history),
                        "sha256": _digest(_json(history)),
                        "source": "private_ram"
                        if isinstance(first["history"], list)
                        else "saved",
                    },
                    agent={
                        "present": first["agent_present"],
                        "model": first["model"],
                        "provider": first["provider"],
                        "route_source": first["route_source"],
                        "history_count": len(messages),
                        "history_sha256": _digest(_json(messages))
                        if first["agent_present"]
                        else None,
                        "system_prompt_sha256": _digest(_json(first["prompt"]))
                        if first["agent_present"]
                        else None,
                    },
                    private={
                        "history_present": isinstance(first["history"], list),
                        "store_count": first["private_store_count"]
                        + first["snapshot_count"]
                        + first["parked_agent_count"]
                        + int(isinstance(first["history"], list)),
                    },
                )
                if probe is not None:
                    result["recall"] = recall_witness(
                        messages,
                        boundary=boundary,
                        saved=saved,
                        session_id=first["session_id"],
                        provider=first["memory_provider"],
                        probe=probe,
                        private=first["incognito"],
                        busy=first["busy"],
                    )
                if observe(key)["token"] != first["token"]:
                    raise RuntimeError("Session snapshot unavailable")
                return result
        except Exception:
            raise ValueError("Session snapshot unavailable") from None

    return handler
