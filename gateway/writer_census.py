"""Best-effort, read-only Hindsight writer ownership over existing gateway containers.

No heap, frame or writer-target scan: an orphan writer must remain unaccounted.
All returned values are plain data, never owners, callbacks or exception objects.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import weakref
from collections import Counter
from typing import Any

_CATEGORIES = ("cached_idle", "turn_unfinished", "release_in_progress", "other_owner",
               "writerless_provider")
_PRIORITY = {name: rank for rank, name in enumerate(
    ("release_in_progress", "turn_unfinished", "cached_idle", "other_owner"))}


def _copy(container, label, raced, lock=None):
    """Never queue behind a lifecycle lock or retry a changing container."""
    if container is None:
        return []
    if lock is not None and not lock.acquire(blocking=False):
        raced.add(f"{label}:lock_busy")
        return []
    try:
        try:
            return list(container.items())
        except RuntimeError:
            raced.add(f"{label}:changed_during_copy")
            return []
    finally:
        if lock is not None:
            lock.release()


def _key_hash(key):
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]


def _unfinished(agent):
    lifetime = getattr(agent, "_gateway_turn_lifetime", None)
    return (isinstance(lifetime, tuple) and len(lifetime) == 2
            and not all(getattr(stage, "is_set", lambda: True)() for stage in lifetime))


def _agent_owner(agent, structure, key, category, add_manager):
    if agent is None:
        return
    if getattr(agent, "_gateway_soft_release_started", False) is True:
        category = "release_in_progress"
    elif _unfinished(agent):
        category = "turn_unfinished"
    add_manager(getattr(agent, "_memory_manager", None), structure, key, category,
                getattr(agent, "_memory_provider_shutdown", False) is True, id(agent))


def _adapter_owners(runner, raced, add_manager):
    adapters = [a for _, a in _copy(getattr(runner, "adapters", None), "adapters", raced)]
    for _, group in _copy(getattr(runner, "_profile_adapters", None), "_profile_adapters", raced):
        adapters.extend(a for _, a in _copy(group, "_profile_adapters.group", raced))
    seen = set()
    for adapter in adapters:
        if id(adapter) in seen:
            continue
        seen.add(id(adapter))
        for attr in ("_active_run_agents", "_shutdown_interruptible_agents"):
            for key, agent in _copy(getattr(adapter, attr, None), f"adapters.{attr}", raced):
                _agent_owner(agent, f"adapters.{attr}", key, "other_owner", add_manager)
        registry = getattr(adapter, "_memory_sessions", None)
        for key, entry in _copy(getattr(registry, "_entries", None), "api_memory_sessions", raced,
                                getattr(registry, "_lock", None)):
            add_manager(entry[0], "adapters._memory_sessions._entries", key, "other_owner")


def _release_owners(threads, raced, add_manager):
    """Recognize the existing release wrapper's arguments, never arbitrary thread targets."""
    for thread in threads:
        args = getattr(thread, "_args", ())
        if not isinstance(args, tuple) or not args:
            continue
        if thread.name == "api-server-memory-evict" and len(args) == 2:
            add_manager(args[0], "api_server_memory_sessions.retirement_thread.args", None,
                        "release_in_progress")
            continue
        if getattr(args[0], "__name__", "") != "_run_release_in_profile_scope" or len(args) != 4:
            if thread.name == "agent-cache-pressure":
                # Its current agent and retired managers live only in stack locals. Do not hide that gap.
                raced.add("agent_cache_pressure:stack_local_owners_not_scanned")
            continue
        _, target, owned_args, key = args
        method = getattr(target, "__name__", "")
        if not owned_args:
            continue
        if method == "_shutdown_memory_manager":
            add_manager(owned_args[0], "release_thread.args.manager", key, "release_in_progress")
        elif method in {"_release_evicted_agent_soft", "_commit_then_release_soft"}:
            agent = owned_args[0]
            _agent_owner(agent, "release_thread.args.agent", key, "release_in_progress", add_manager)
            if getattr(agent, "_memory_manager", None) is None:
                raced.add("release_thread:detached_stack_local_manager_not_scanned")


def _collect_owners(runner, threads, raced, add_manager):
    cache = getattr(runner, "_agent_cache", None)
    cached = _copy(cache, "_agent_cache", raced, getattr(runner, "_agent_cache_lock", None))
    for key, entry in cached:
        if isinstance(entry, tuple) and entry:
            _agent_owner(entry[0], "_agent_cache", key, "cached_idle", add_manager)
    for key, state in _copy(getattr(runner, "_sessions", None), "_sessions", raced):
        turn = getattr(state, "turn", None)
        _agent_owner(getattr(turn, "agent", None), "_sessions.turn.agent", key, "other_owner", add_manager)
        holder = getattr(getattr(turn, "ctx", None), "agent_holder", None)
        if holder:
            _agent_owner(holder[0], "_sessions.turn.ctx.agent_holder", key, "other_owner", add_manager)
    for future, agent in _copy(getattr(runner, "_deferred_agent_workers", None),
                               "_deferred_agent_workers", raced):
        _agent_owner(agent, "_deferred_agent_workers", id(future), "other_owner", add_manager)
    _adapter_owners(runner, raced, add_manager)
    _release_owners(threads, raced, add_manager)
    # Cache replacements (including same-size changes) must not masquerade as a stable snapshot.
    after = _copy(cache, "_agent_cache", raced, getattr(runner, "_agent_cache_lock", None))
    if [(key, id(entry)) for key, entry in cached] != [(key, id(entry)) for key, entry in after]:
        raced.add("_agent_cache:changed_during_snapshot")


def writer_census(runner: Any) -> dict[str, Any]:
    """One bounded observation, not an atomic stop-the-world or a drain promise."""
    from gateway.status import _get_code_identity_fields, get_process_start_time

    wall, started = time.time(), time.monotonic()
    threads = threading.enumerate()
    writers = [thread for thread in threads if thread.name == "hindsight-writer" and thread.is_alive()]
    inventory = {id(thread): {"thread_id": id(thread), "ident": thread.ident,
                              "native_id": thread.native_id, "name": thread.name,
                              "started_monotonic_ns": getattr(thread, "_census_started_ns", None),
                              "is_alive": thread.is_alive(), "daemon": thread.daemon}
                 for thread in writers}
    raced: set[str] = set()
    providers: dict[int, dict] = {}
    observed: dict[int, weakref.ReferenceType] = {}

    def add_manager(manager, structure, key, category, shutdown=False, agent_id=None):
        if manager is None:
            return
        source = getattr(manager, "_providers", None) or ()
        try:
            before = tuple(source)
        except TypeError:
            raced.add("memory_manager:providers_unreadable")
            return
        for provider in before:
            if getattr(provider, "name", None) != "hindsight":
                continue
            previous = observed.get(id(provider))
            if previous is not None and previous() is not provider:
                raced.add("provider:object_id_reused_during_snapshot")
            try:
                observed[id(provider)] = weakref.ref(provider)
            except TypeError:
                # Not weakly referenceable: its identity cannot be checked across the snapshot.
                raced.add("provider:not_weakly_referenceable")
            writer = getattr(provider, "_writer_thread", None)
            event = getattr(provider, "_shutting_down", None)
            stopped = shutdown or getattr(manager, "_shutdown_started", False) is True
            stopped = stopped or (event is not None and event.is_set())
            owner = {"structure": structure, "key_hash": _key_hash(key), "agent_id": agent_id,
                     "manager_id": id(manager)}
            row = providers.get(id(provider))
            if row is None:
                row = providers[id(provider)] = {"provider_id": id(provider), "category": category,
                    "lifetime_monotonic_ns": getattr(provider, "_census_lifetime_ns", None),
                    "writer_id": id(writer) if writer is not None else None,
                    "shutdown": bool(stopped), "owners": []}
            if owner not in row["owners"]:
                row["owners"].append(owner)
            if _PRIORITY[category] < _PRIORITY[row["category"]]:
                row["category"] = category
            row["shutdown"] |= bool(stopped)
            if row["writer_id"] != (id(writer) if writer is not None else None):
                raced.add("provider:writer_changed_during_snapshot")
            if writer is None or not getattr(writer, "is_alive", lambda: False)():
                # Never started, or exited (shutdown() keeps the handle): no live writer owned here.
                row["writerless"] = True
        if tuple(id(p) for p in source) != tuple(id(p) for p in before):
            raced.add("memory_manager:providers_changed_during_snapshot")

    _collect_owners(runner, threads, raced, add_manager)
    if any(ref() is None for ref in observed.values()):
        raced.add("provider:disappeared_during_snapshot")
    references: dict[int, list[int]] = {}
    for row in providers.values():
        if row.pop("writerless", False):
            row["category"] = "writerless_provider"
        writer_id = row["writer_id"]
        if writer_id is not None:
            references.setdefault(writer_id, []).append(row["provider_id"])
    matched, unaccounted, duplicate, shutdown_alive = [], [], [], []
    for writer_id, writer in inventory.items():
        owner_ids = sorted(references.get(writer_id, []))
        writer["provider_ids"] = owner_ids
        if len(owner_ids) == 1:
            matched.append(writer_id)
        elif not owner_ids:
            unaccounted.append(writer_id)
        else:
            duplicate.append(writer_id)
        if any(providers[p]["shutdown"] for p in owner_ids):
            shutdown_alive.append(writer_id)
    current = {id(t) for t in threading.enumerate() if t.name == "hindsight-writer" and t.is_alive()}
    if current != set(inventory):
        raced.add("threads:changed_during_snapshot")
    unhealthy = bool(unaccounted or duplicate or shutdown_alive)
    counts = Counter(row["category"] for row in providers.values())
    process_start = get_process_start_time(os.getpid())
    identity_complete = (process_start is not None
                         and all(row["started_monotonic_ns"] is not None for row in inventory.values())
                         and all(row["lifetime_monotonic_ns"] is not None for row in providers.values()))
    for row in providers.values():
        row["owners"].sort(key=lambda owner: (owner["structure"], owner["key_hash"]))
    return {"wall_time": wall, "monotonic_time": started, "pid": os.getpid(),
            "process_start_time": process_start, "identity_complete": identity_complete,
            **_get_code_identity_fields(), "duration_seconds": time.monotonic() - started,
            "incomplete": bool(raced), "raced": sorted(raced),
            "health": "unhealthy" if unhealthy else "incomplete" if raced else "healthy",
            "counts": {**dict.fromkeys(_CATEGORIES, 0), **counts, "providers": len(providers),
                       "live_writers": len(inventory), "matched": len(matched),
                       "unaccounted_writers": len(unaccounted), "duplicate_owner": len(duplicate),
                       "shutdown_but_alive": len(shutdown_alive)},
            "writers": sorted(inventory.values(), key=lambda row: row["thread_id"]),
            "providers": sorted(providers.values(), key=lambda row: row["provider_id"]),
            "matched": sorted(matched), "unaccounted_writers": sorted(unaccounted),
            "duplicate_owner": sorted(duplicate), "shutdown_but_alive": sorted(shutdown_alive)}
