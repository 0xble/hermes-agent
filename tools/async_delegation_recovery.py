"""Async-delegation restart recovery, completion ownership and delivery reconciliation."""

from __future__ import annotations

import logging
from typing import Any, Optional


logger = logging.getLogger('tools.async_delegation')


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown; children a multi-child unit had already
    recorded (``record_unit_child``) are replayed with their real results."""
    from tools.async_delegation import (
        _DB_LOCK, _ROUTING_KEYS, _owner_liveness, _transaction, json, recovered_children,
        redact_child_result, redact_completion_event, require_ledger_owner, time,
    )
    require_ledger_owner()
    alive = _owner_liveness()
    if alive is None:
        return 0
    now, recovered = time.time(), 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id, result_json, state
               FROM async_delegations WHERE state IN ('queued','admitted','running','finalizing')""").fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json, origin_sid, result_json, last_state = row
            if alive(pid, started):
                continue
            task = json.loads(task_json or "{}")
            cron_execution_id = task.get("cron_execution_id")
            if cron_execution_id:
                # A cron runner is merely a waiter. Its detached worker owns the
                # execution and can outlive this process. Do not classify the
                # waiter's queued/admitted handoff as never-started work.
                from cron.delivery_queue import MISSING_EXECUTION_GRACE
                from cron.executions import get_execution
                execution = get_execution(cron_execution_id)
                if execution is None:
                    if now - dispatched_at < MISSING_EXECUTION_GRACE.total_seconds():
                        if last_state in ("queued", "admitted"):
                            conn.execute("""UPDATE async_delegations SET state='running', updated_at=?
                                   WHERE delegation_id=? AND state=?""", (now, delegation_id, last_state))
                        continue
                    # A pruned row has no provable outcome. Use the generic
                    # unknown event below; never rerun or invent a completion.
                elif execution["status"] not in ("completed", "failed", "unknown"):
                    # The scheduler owns ledger recovery on its tick. A delegation
                    # sweep must not mutate the cron store under its own DB lock.
                    if last_state in ("queued", "admitted"):
                        conn.execute("""UPDATE async_delegations SET state='running', updated_at=?
                               WHERE delegation_id=? AND state=?""", (now, delegation_id, last_state))
                    continue
                else:
                    from tools.cronjob_tools import _manual_run_completion
                    completed = _manual_run_completion(
                        {}, task["cron_job_id"], task["cron_job_name"],
                        task["cron_deliver"], dispatched_at,
                        execution_id=cron_execution_id,
                    )
                    event = {
                        "type": "async_delegation", "delegation_id": delegation_id,
                        "session_key": session_key, "origin_ui_session_id": origin_ui,
                        "origin_session_id": origin_sid or "", "parent_session_id": parent_id,
                        "goal": task.get("goal", ""), "context": task.get("context"),
                        "toolsets": task.get("toolsets"), "role": task.get("role"),
                        "model": task.get("model"), **completed,
                        "dispatched_at": dispatched_at, "completed_at": now,
                        **{k: task[k] for k in _ROUTING_KEYS if task.get(k)},
                    }
                    conn.execute("""UPDATE async_delegations SET state=?, completed_at=?,
                           updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                           WHERE delegation_id=? AND state IN ('queued','running','finalizing','admitted')""",
                        (completed["status"], now, now, json.dumps(event),
                         json.dumps(completed), delegation_id))
                    recovered += 1
                    continue
            if last_state in ("queued", "admitted") and not cron_execution_id:
                error = f"Work was {last_state}; it was never started."
                event = {
                    "type": "async_delegation", "delegation_id": delegation_id,
                    "session_key": session_key, "origin_ui_session_id": origin_ui,
                    "origin_session_id": origin_sid or "", "parent_session_id": parent_id,
                    "goal": task.get("goal", ""), "goals": task.get("goals"),
                    "context": task.get("context"), "toolsets": task.get("toolsets"),
                    "role": task.get("role"), "model": task.get("model"),
                    "is_batch": bool(task.get("is_batch")), "status": "interrupted",
                    "summary": None, "error": error, "exit_reason": "interrupted",
                    "dispatched_at": dispatched_at, "completed_at": now,
                    **{k: task[k] for k in _ROUTING_KEYS if task.get(k)},
                }
                result = {"status": "interrupted", "summary": None, "error": error,
                          "exit_reason": "interrupted"}
                conn.execute("""UPDATE async_delegations SET state='interrupted', completed_at=?,
                       updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                       WHERE delegation_id=? AND state=?""",
                    (now, now, json.dumps(event), json.dumps(result), delegation_id, last_state))
                recovered += 1
                continue
            error = ("Cron execution record missing; outcome unknown."
                     if cron_execution_id else
                     "No terminal result was recorded; outcome unknown.")
            recovered_results = recovered_children(task, result_json, error)
            if recovered_results:
                done = sum(1 for r in recovered_results if r.get("status") != "unknown")
                error = (f"The unit did not finish; {done}/{len(recovered_results)} child "
                         "results were recorded and are included below, the rest are unknown.")
            diagnostics = {"last_known_status": last_state, "task_transcripts": task.get("task_transcripts") or {}}
            # Verbatim transcript tails + a git snapshot of the owner's cwd, so the parent can
            # continue or re-dispatch from the event alone instead of opening files (#116000).
            from tools.async_delegation_recovery_hints import git_state_hint, transcript_tails
            if tails := transcript_tails(diagnostics["task_transcripts"]):
                diagnostics["transcript_tails"] = tails
            if hint := git_state_hint(task.get("owner_cwd")):
                diagnostics["git_state_hint"] = hint
            event = {
                "type": "async_delegation", "delegation_id": delegation_id, "session_key": session_key,
                "origin_ui_session_id": origin_ui, "origin_session_id": origin_sid or "",
                "parent_session_id": parent_id, "goal": task.get("goal", ""), "goals": task.get("goals"),
                "context": task.get("context"), "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "unknown", "summary": None, "error": error, **diagnostics,
                **({"results": recovered_results} if recovered_results else {}),
                "dispatched_at": dispatched_at, "completed_at": now,
                **{k: task[k] for k in _ROUTING_KEYS if task.get(k)}}
            result = {"status": "unknown", "summary": None, "error": event["error"], **diagnostics,
                      **({"results": recovered_results} if recovered_results else {})}
            event, result = redact_completion_event(event), redact_child_result(result)
            conn.execute("""UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""", (now, now, json.dumps(event), json.dumps(result), delegation_id))
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.
    Restored events are stamped ``restored=True`` in memory only: they came from a PREVIOUS
    process, so drains without an ownership filter must leave them for a consumer that can
    prove ownership. Rows older than ``_MAX_COMPLETION_REPLAY_AGE_S`` are terminally dropped
    instead of replaying a turn nobody is waiting on.

    Every restored event is stamped ``restored=True`` (in-memory only — the stamp is added after the durable
    payload is deserialized and is never persisted). Restored events originate from a *previous* process, so
    no consumer in THIS process implicitly owns them: drain paths that run without an ownership filter (the
    legacy single-session behavior) must leave them queued for a consumer that can positively prove
    ownership, otherwise a brand-new session adopts a dead session's delegation results seconds after boot
    (#64484).
    """
    from tools.async_delegation import (
        _DB_LOCK, _db_path, _outbox_delivery_rows, _replay_pending, _transaction,
        recover_abandoned_delegations, replay_outbox_pending, time,
    )
    from agent.runtime_session_store import is_worker_process
    if is_worker_process():
        # The ordinary owner restores delivery-only results; a compute import
        # must neither reap that owner's ledger nor steal its delivery queue.
        return 0
    if not _db_path().exists():
        return 0  # nothing to replay; a replay must not create (or migrate) the ledger (#123265)
    recover_abandoned_delegations()
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id""").fetchall()
        restored = _replay_pending(conn, rows, target_queue, now, startup=True)
        restored += replay_outbox_pending(conn, _outbox_delivery_rows(conn), target_queue, now)
        return restored


def _replay_pending(conn, rows, target_queue, now: float, *, startup: bool = False) -> int:
    """Put each pending ``(delegation_id, event_json, completed_at, dispatched_at)`` row on ``target_queue``
    stamped ``restored``, or terminally drop it past ``_MAX_COMPLETION_REPLAY_AGE_S``. Records the offer so
    the orphan sweep skips the row until the copy is handed back (``return_completion_offer``)."""
    from tools.async_delegation import (
        _MAX_COMPLETION_REPLAY_AGE_S, _offered, _orphan_lock, _stamp_resume_availability,
        _startup_replayed, get_hermes_home, hermes_home_key, json, redact_completion_event,
    )
    home, restored = hermes_home_key(get_hermes_home()), 0
    for delegation_id, payload, completed_at, dispatched_at in rows:
        age_basis = completed_at or dispatched_at
        if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
            conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                          delivery_claim=NULL, delivery_claimed_at=NULL,
                          updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'""", (now, delegation_id))
            logger.warning("Async delegation %s: pending completion is %.1fh old "
                           "(cap %.1fh); terminally dropping the replay (result remains queryable).",
                           delegation_id, (now - age_basis) / 3600.0, _MAX_COMPLETION_REPLAY_AGE_S / 3600.0)
            continue
        with _orphan_lock:
            if (home, delegation_id) in _offered:
                # The owner's explicit startup replay and the first-consume replay
                # (process_registry.restore_completions) may both run: offer once.
                continue
        evt = json.loads(payload)
        if isinstance(evt, dict):
            evt = redact_completion_event(evt)
            _stamp_resume_availability(evt, conn)
        with _orphan_lock:
            if startup:
                _startup_replayed.add(delegation_id)
            if isinstance(evt, dict):
                evt["restored"] = True
                if delegation_id in _startup_replayed:
                    evt["startup_replay"] = True
        target_queue.put(evt)
        with _orphan_lock:
            _offered.add((home, delegation_id))
        restored += 1
    return restored


def completion_copy_id(evt: Any) -> str:
    """The delegation id ``evt`` is the final-result copy of, or "" (other events, interim notices)."""
    from tools.async_delegation import is_interim_delegation_event
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation" or is_interim_delegation_event(evt):
        return ""
    return str(evt.get("delegation_id") or "")


def mark_copy_held(delegation_id: str) -> None:
    """Record that a consumer holds a copy of ``delegation_id`` off every queue (a poller between dequeue and
    requeue, a parked copy). The orphan sweep treats a held copy like a queued one. A holder that moves a copy
    onto a queue marks before it dequeues, or under the queue's mutex, and unmarks only after any requeue, so
    the sweep never sees the copy as neither queued nor held. Pair every call with ``unmark_copy_held``."""
    from tools.async_delegation import _held_copies, _held_lock
    if delegation_id:
        with _held_lock:
            _held_copies[delegation_id] += 1


def unmark_copy_held(delegation_id: str) -> None:
    """End one ``mark_copy_held`` hold on ``delegation_id``."""
    from tools.async_delegation import _held_copies, _held_lock
    if delegation_id:
        with _held_lock:
            _held_copies[delegation_id] -= 1
            if _held_copies[delegation_id] <= 0:
                del _held_copies[delegation_id]


def _queued_completion_ids(*queues) -> Optional[set]:
    """Delegation ids with a final-result copy sitting in any of ``queues`` or held off-queue by a consumer
    (``mark_copy_held``), or None when a queue cannot be inspected. Each queue is read under its own mutex
    (the lock ``put``/``get`` and ``take_ready`` take) with the held ids read inside it, so a copy moving
    between a queue and a hold is always seen in one of them. Interim per-task notices share their batch's
    id but are not the result, so they never count as its copy."""
    from tools.async_delegation import _held_copies, _held_lock, completion_copy_id
    ids: set = set()
    for q in {id(q): q for q in queues if q is not None}.values():
        mutex, items = getattr(q, "mutex", None), getattr(q, "queue", None)
        if mutex is None or items is None:
            return None
        with mutex:
            snapshot = list(items)
            with _held_lock:
                ids.update(_held_copies)
        ids.update(completion_copy_id(evt) for evt in snapshot)
    ids.discard("")
    return ids


def _copy_may_be_live(delegation_id: str, pid, updated_at: float, live_ids: set, home: str,
                      offered: set, now: float) -> bool:
    """For a row whose copy this process made (it owns the row or offered it): whether that copy may still be
    delivered, so the sweep must not add another. Live = queued or held (``live_ids``), or the row or this
    process's last re-offer was touched within a claim lease, the span after which a silent claim counts as
    abandoned. A row owned by ANOTHER live process is left to it: its queue is out of sight."""
    from tools.async_delegation import _CLAIM_LEASE_S, _orphan_lock, _reoffered_at, os
    if not (delegation_id in offered or int(pid) == os.getpid()):
        return True
    if delegation_id in live_ids or updated_at >= now - _CLAIM_LEASE_S:
        return True
    with _orphan_lock:
        return _reoffered_at.get((home, delegation_id), float("-inf")) >= now - _CLAIM_LEASE_S


def _note_reoffers(home: str, lost: list, now: float) -> None:
    """Start each lost copy's re-offer lease and log it: WARNING the first time a row is re-offered, DEBUG
    after that (a consumer that keeps handing the copy back would otherwise warn once per lease)."""
    from tools.async_delegation import (
        _MAX_COMPLETION_REPLAY_AGE_S, _offered, _orphan_lock, _reoffered_at,
    )
    with _orphan_lock:
        first = [d for d in lost if (home, d) not in _reoffered_at]
        for key in [k for k, at in _reoffered_at.items() if now - at > _MAX_COMPLETION_REPLAY_AGE_S]:
            del _reoffered_at[key]  # its row is past the replay cap, so it was dropped
        _reoffered_at.update({(home, d): now for d in lost})
        _offered.difference_update({(home, d) for d in lost})
    log = logger.warning if first else logger.debug
    log("Re-offering %d pending async completion(s) whose in-memory copy was lost while this process ran: %s",
        len(lost), ", ".join(lost))


def sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """Re-offer this home's pending completions that no live in-memory copy will deliver (#97202, #140718).

    Startup replay (``restore_undelivered_completions``) covers owners that died before the process
    started; this covers the rest while it runs. Abandoned in-flight rows are first classified by
    ``recover_abandoned_delegations``. A terminal row qualifies when it is pending with an event, idle
    past ``_ORPHAN_STALE_S`` and not under a live delivery claim, and then:

    - its owner is dead and this process has not offered it yet; or
    - its owner is THIS process, or this process already offered it, the row has been idle for a whole
      ``_CLAIM_LEASE_S`` and no copy of it sits in ``target_queue`` or the producers'
      ``completion_queue``: the one in-memory copy was lost (a consumer discarded it, or it went onto
      a queue no delivery loop drains), and without this the row would stay pending with zero
      attempts until a restart (#144563).

    The sweep never adds a copy beside a live one: queued, held off-queue by a consumer
    (``mark_copy_held``), under a claim lease (filtered above), or touched within a lease (a claim, defer
    or release refreshes ``updated_at``). Each re-offer starts its own lease, so a consumer that keeps
    handing the copy back sees it at most once per lease, and only the first re-offer warns. A row owned
    by ANOTHER live process is left to that process, whose queue this one cannot see. The consumer's
    ``claim_completion_delivery`` stays the atomic cross-process gate, so a duplicate copy can never
    deliver twice. Rows past the delivery budget or the replay age converge to ``dropped``. Reads the
    current profile's ledger: callers bind the owning profile first."""
    from tools.async_delegation import (
        _CLAIM_LEASE_S, _DB_LOCK, _MAX_DELIVERY_ATTEMPTS, _ORPHAN_STALE_S, _copy_may_be_live, _db_path,
        _note_reoffers, _offered, _orphan_lock, _owner_liveness, _queued_completion_ids, _replay_pending,
        _transaction, get_hermes_home, hermes_home_key, recover_abandoned_delegations,
        reoffer_unresolved_completions, replay_outbox_pending, time,
    )
    held = reoffer_unresolved_completions(target_queue)  # unproven terminal events whose retry is due
    alive = _owner_liveness()
    if alive is None or not _db_path().exists():
        return held  # never create a ledger just to sweep it
    recover_abandoned_delegations()
    now = time.time() if now is None else now
    home = hermes_home_key(get_hermes_home())
    from tools.process_registry import process_registry
    live = _queued_completion_ids(target_queue, process_registry.completion_queue)
    with _orphan_lock:
        offered = {delegation_id for key, delegation_id in _offered if key == home}
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at,
                      owner_pid, owner_started_at, delivery_attempts, updated_at
               FROM async_delegations
               WHERE state NOT IN ('admitted','running','finalizing') AND delivery_state='pending'
                 AND event_json IS NOT NULL AND updated_at < ?
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
               ORDER BY completed_at, delegation_id""", (now - _ORPHAN_STALE_S, now - _CLAIM_LEASE_S)).fetchall()
        orphans, lost = [], []
        for delegation_id, payload, completed_at, dispatched_at, pid, started, attempts, updated_at in rows:
            copy_lost = alive(pid, started) or delegation_id in offered
            if copy_lost and (live is None or _copy_may_be_live(delegation_id, pid, updated_at, live, home,
                                                                  offered, now)):
                continue
            if (attempts or 0) >= _MAX_DELIVERY_ATTEMPTS:
                # Its last claimant died holding the final attempt; converge like release_completion_delivery.
                conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""", (now, delegation_id))
                logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                               "marking terminally dropped (result remains queryable).",
                               delegation_id, _MAX_DELIVERY_ATTEMPTS)
                continue
            orphans.append((delegation_id, payload, completed_at, dispatched_at))
            if copy_lost:
                lost.append(delegation_id)
        if lost:
            _note_reoffers(home, lost, now)
        restored = _replay_pending(conn, orphans, target_queue, now)
        outbox_rows = conn.execute(
            """SELECT e.event_id, e.event_json, e.created_at, e.updated_at,
                      d.owner_pid, d.owner_started_at, e.delivery_attempts
               FROM async_delegation_events e
               LEFT JOIN async_delegations d ON d.delegation_id=e.delegation_id
               WHERE e.delivery_state='pending' AND e.event_json IS NOT NULL AND e.updated_at < ?
                 AND (e.delivery_claim IS NULL OR e.delivery_claimed_at < ?)
               ORDER BY e.created_at, e.event_id""",
            (now - _ORPHAN_STALE_S, now - _CLAIM_LEASE_S),
        ).fetchall()
        outbox_orphans = []
        for event_id, payload, created_at, updated_at, pid, started, attempts in outbox_rows:
            if event_id in offered or alive(pid, started):
                continue
            if (attempts or 0) >= _MAX_DELIVERY_ATTEMPTS:
                conn.execute(
                    """UPDATE async_delegation_events SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                       WHERE event_id=? AND delivery_state='pending'""",
                    (now, event_id),
                )
                continue
            outbox_orphans.append((event_id, payload, created_at, updated_at))
        return held + restored + replay_outbox_pending(conn, outbox_orphans, target_queue, now)


def maybe_sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """``sweep_orphaned_completions`` at most once per ``ORPHAN_SWEEP_INTERVAL_S`` per home (``now`` is
    monotonic), for delivery loops that tick far more often. Never raises into the loop."""
    from tools.async_delegation import (
        ORPHAN_SWEEP_INTERVAL_S, _last_orphan_sweep, _orphan_lock, get_hermes_home, hermes_home_key,
        sweep_orphaned_completions, time,
    )
    home = hermes_home_key(get_hermes_home())
    now = time.monotonic() if now is None else now
    with _orphan_lock:
        last = _last_orphan_sweep.get(home)
        if last is not None and now - last < ORPHAN_SWEEP_INTERVAL_S:
            return 0
        _last_orphan_sweep[home] = now
    try:
        return sweep_orphaned_completions(target_queue)
    except Exception:
        logger.debug("Orphaned async delegation sweep failed", exc_info=True)
        return 0


def _update_delivery(sql: str, params: tuple) -> bool:
    """Run one UPDATE on the ledger; True iff exactly one row changed."""
    from tools.async_delegation import _DB_LOCK, _transaction
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(sql, params).rowcount == 1


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    from tools.async_delegation import _update_delivery, require_ledger_owner, time
    require_ledger_owner()
    now = time.time()
    return _update_delivery("""UPDATE async_delegations SET delivery_state='delivered',
                   delivered_at=?, updated_at=?
           WHERE delegation_id=? AND delivery_state!='delivered'""", (now, now, delegation_id))


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    from tools.async_delegation import _CLAIM_LEASE_S, _DB_LOCK, _transaction, time, worker_ledger
    if remote := worker_ledger():
        return remote.claim(delegation_id, claim_id)
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute("""UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - _CLAIM_LEASE_S))
        return cur.rowcount == 1


def _hold_unresolved(evt: dict[str, Any], meta: dict[str, Any], exc: BaseException) -> None:
    from tools.async_delegation import (
        _OWNERSHIP_RETRY_BASE_S, _OWNERSHIP_RETRY_MAX_S, _TERMINAL_OWNERSHIP_KEY, _orphan_lock,
        _unresolved, time,
    )
    meta["attempts"] = int(meta.get("attempts") or 0) + 1
    delay = min(_OWNERSHIP_RETRY_BASE_S * 2 ** (meta["attempts"] - 1), _OWNERSHIP_RETRY_MAX_S)
    meta["retry_at"] = time.time() + delay
    evt[_TERMINAL_OWNERSHIP_KEY] = meta
    logger.error("Async delegation %s: terminal ownership is still unproven (attempt %d, retry in %.0fs); "
                 "holding the result undelivered: %s", evt.get("delegation_id"), meta["attempts"], delay, exc)
    with _orphan_lock:
        _unresolved[(meta["home"], str(evt.get("delegation_id") or ""))] = evt


def reoffer_unresolved_completions(target_queue, *, now: Optional[float] = None) -> int:
    """Put held terminal events whose ownership retry is due back on ``target_queue``."""
    from tools.async_delegation import _TERMINAL_OWNERSHIP_KEY, _orphan_lock, _unresolved, time
    now = time.time() if now is None else now
    with _orphan_lock:
        due = [key for key, evt in _unresolved.items()
               if (evt.get(_TERMINAL_OWNERSHIP_KEY) or {}).get("retry_at", 0.0) <= now]
        events = [_unresolved.pop(key) for key in due]
    for evt in events:
        target_queue.put(evt)
    return len(events)


def _resolve_terminal_ownership(evt: dict[str, Any], meta: dict[str, Any]) -> str:
    """Resolve an unproven terminal event to its durable identity, acquiring the lifecycle row
    through the conditional fallback transaction when no terminal owner exists yet."""
    from tools.async_delegation import _persist_outbox_event, _reconcile_terminal_write
    # The producer's snapshots, not ``evt``: consumers enrich their copy (e.g. gateway routing
    # fields) before claiming, and the durable payloads were written from the originals.
    event, persisted, result, expected = meta["event"], meta["persisted_event"], meta["result"], meta["expected_state"]
    disposition = _reconcile_terminal_write(event, result, expected, persisted)
    if disposition == "active":
        try:
            _persist_outbox_event(event, result, event_kind="terminal_fallback",
                                  terminal_status=meta["terminal_status"], expected_state=expected)
            return "outbox"
        except Exception as exc:
            logger.error("Async delegation %s: deferred terminal fallback write failed; reconciling: %s",
                         evt.get("delegation_id"), exc)
            disposition = _reconcile_terminal_write(event, result, expected, persisted)
            if disposition == "active":
                raise
    return disposition


def resolve_event_ownership(evt: dict[str, Any]) -> bool:
    """True when ``evt`` may be delivered: it carries no unproven terminal ownership, or that
    ownership now resolves to this event. A proven loser remains permanently rejected, including
    on copies/requeues. An event whose ledger is still unreadable is held and re-offered later
    (``reoffer_unresolved_completions``); neither may be shown by this caller."""
    from tools.async_delegation import (
        _TERMINAL_OWNERSHIP_KEY, _TERMINAL_STATES, _hold_unresolved, _outbox_event_id, _record_context_run,
        _records, _records_lock, _resolve_terminal_ownership, durable_status, get_hermes_home,
        hermes_home_key, sync_completion_payload,
    )
    meta = evt.get(_TERMINAL_OWNERSHIP_KEY)
    if not meta:
        return True
    if meta.get("rejected"):
        return False
    evt.pop("_delivery_event_id", None)
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(meta["home"])
    try:
        disposition = _resolve_terminal_ownership(evt, meta)
    except Exception as exc:
        _hold_unresolved(evt, meta, exc)
        return False
    finally:
        reset_hermes_home_override(token)
    if disposition == "outbox":
        evt["_delivery_event_id"] = _outbox_event_id(evt, "terminal_fallback")
    elif disposition not in {"lifecycle", "missing"}:
        # A failed batch can requeue this very dict after skipping it. Keep rejection
        # in the existing marker (shared by shallow copies), not in the mutable ledger.
        meta["rejected"] = True
        logger.error("Async delegation %s: unproven terminal result lost ownership to a competing "
                     "result; permanently rejecting it", evt.get("delegation_id"))
        return False
    sync_completion_payload(evt, meta["event"])
    with _records_lock:
        live = _records.get(evt.get("delegation_id"))
        if live and live.get("status") in {*_TERMINAL_STATES, "finalizing"} and hermes_home_key(_record_context_run(live, get_hermes_home)) == hermes_home_key(meta["home"]):
            live.update(_completion_status=evt["status"], _terminal_state=durable_status(evt, meta["terminal_status"]))
            if live["status"] != "finalizing":
                live.update(status=live["_completion_status"], _durable_state=live["_terminal_state"])
    evt.pop(_TERMINAL_OWNERSHIP_KEY, None)
    return True


def is_interim_delegation_event(evt: dict[str, Any]) -> bool:
    """An early per-task notice for a batch that is still running.

    It has its own durable outbox identity, so claiming or acknowledging it can
    never consume the batch's terminal completion row.
    """
    return evt.get("type") == "async_delegation" and bool(evt.get("task_failure_notice"))


def claim_event_delivery(evt: dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; legacy in-memory notices remain unclaimable.

    ``None`` means this copy must not be delivered: another consumer holds it, or its terminal
    ownership is unproven (held for a later offer) or lost to a competing writer."""
    from tools.async_delegation import (
        _attach_retry_note, _claim_outbox_delivery, claim_completion_delivery, is_interim_delegation_event,
        os, resolve_event_ownership, uuid,
    )
    if not resolve_event_ownership(evt):
        return None
    _attach_retry_note(evt)
    event_id = str(evt.get("_delivery_event_id") or "")
    if event_id:
        return _claim_outbox_delivery(event_id, consumer)
    if is_interim_delegation_event(evt):
        return ""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry. Attempts are
    counted at claim time; once the budget is exhausted the row converges to
    terminal ``dropped`` (only pending rows replay on restart)."""
    from tools.async_delegation import (
        _DB_LOCK, _MAX_DELIVERY_ATTEMPTS, _outbox_event_id_from_claim, _transaction,
        _update_outbox_delivery, time, worker_ledger,
    )
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "release")
    if remote := worker_ledger():
        return remote.release(delegation_id, claim_id)
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        capped = conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS))
        if capped.rowcount == 1:
            logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                           "marking terminally dropped (result remains queryable).",
                           delegation_id, _MAX_DELIVERY_ATTEMPTS)
            return True
        cur = conn.execute("""UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""", (now, delegation_id, claim_id))
        return cur.rowcount == 1


def defer_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Return an unadmitted completion to pending without spending a delivery attempt."""
    from tools.async_delegation import (
        _outbox_event_id_from_claim, _update_delivery, _update_outbox_delivery, time, worker_ledger,
    )
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "defer")
    if remote := worker_ledger():
        return remote.defer(delegation_id, claim_id)
    return _update_delivery("""UPDATE async_delegations SET delivery_claim=NULL,
                  delivery_claimed_at=NULL, delivery_attempts=MAX(0, delivery_attempts-1),
                  updated_at=?
           WHERE delegation_id=? AND delivery_state='pending' AND delivery_claim=?""",
        (time.time(), delegation_id, claim_id))


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion whose target is permanently gone (the
    spawning session ended at an explicit user boundary such as /new or reset).
    ``dropped`` — not ``delivered`` — keeps the ack honest; not ``pending`` keeps
    restart recovery from replaying it into a fail-closed drop forever."""
    from tools.async_delegation import (
        _outbox_event_id_from_claim, _update_delivery, _update_outbox_delivery, time, worker_ledger,
    )
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "drop")
    if remote := worker_ledger():
        return remote.drop(delegation_id, claim_id)
    return _update_delivery("""UPDATE async_delegations SET delivery_state='dropped',
                  updated_at=?, delivery_claim=NULL,
                  delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (time.time(), delegation_id, claim_id))


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    from tools.async_delegation import (
        _outbox_event_id_from_claim, _update_delivery, _update_outbox_delivery, time, worker_ledger,
    )
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "complete")
    if remote := worker_ledger():
        return remote.ack(delegation_id, claim_id)
    now = time.time()
    return _update_delivery("""UPDATE async_delegations SET delivery_state='delivered',
                  delivered_at=?, updated_at=?, delivery_claim=NULL, delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (now, now, delegation_id, claim_id))


def requeue_admitted_completion(delegation_id: str, target_queue, claim_id: str = "") -> bool:
    """Offer an acknowledged completion again when its receiving turn could not confirm the owner.

    The row returns to pending and its event goes back on ``target_queue``, so the next delivery re-runs
    the pre-flight. A row past the delivery budget converges to ``dropped`` (bounding the loop); one without
    a stored event is left. ``claim_id``: an admission claim whose ack may be in flight; its still-pending
    row reopens too and the claim is cleared, so the late ack no-ops instead of overwriting the retry."""
    from tools.async_delegation import (
        _DB_LOCK, _MAX_DELIVERY_ATTEMPTS, _offered, _orphan_lock, _replay_pending, _startup_replayed,
        _transaction, time,
    )
    now, params = time.time(), (delegation_id, claim_id, claim_id)
    admitted = "delegation_id=? AND (delivery_state='delivered' OR (?<>'' AND delivery_state='pending' AND delivery_claim=?))"
    with _DB_LOCK, _transaction() as conn:
        exhausted = conn.execute(f"""UPDATE async_delegations SET delivery_state='dropped', updated_at=?, delivery_claim=NULL,
               delivery_claimed_at=NULL WHERE {admitted} AND delivery_attempts>=?""", (now, *params, _MAX_DELIVERY_ATTEMPTS))
        if exhausted.rowcount == 1:
            logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                           "marking terminally dropped (result remains queryable).",
                           delegation_id, _MAX_DELIVERY_ATTEMPTS)
            return False
        reopened = conn.execute(f"""UPDATE async_delegations SET delivery_state='pending', delivered_at=NULL,
               delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=? WHERE {admitted} AND event_json IS NOT NULL""", (now, *params))
        if reopened.rowcount != 1:
            return False
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchall()
        with _orphan_lock:  # an admitted copy already had its consumer: requeue keeps live routing
            _startup_replayed.discard(delegation_id)
            _offered.difference_update({key for key in _offered if key[1] == delegation_id})
        return _replay_pending(conn, rows, target_queue, now) == 1


def complete_event_delivery(evt: dict[str, Any], claim_id: str) -> None:
    from tools.async_delegation import _event_delivery, complete_completion_delivery
    _event_delivery(complete_completion_delivery, evt, claim_id)


def release_event_delivery(evt: dict[str, Any], claim_id: str) -> None:
    """Release a failed claim for a consumer that discards its copy (the TUI poller): the row is pending
    again, so it must stay eligible for the orphan sweep."""
    from tools.async_delegation import (
        _event_delivery, release_completion_delivery, return_completion_offer,
    )
    _event_delivery(release_completion_delivery, evt, claim_id)
    return_completion_offer(evt)


def return_completion_offer(evt: dict[str, Any]) -> None:
    """Hand an offered completion back to the orphan sweep after its in-memory copy was discarded while
    the durable row stays pending, e.g. a TUI session that cannot prove it owns the event drops it (every
    session poller drains one process-wide queue). The next sweep may offer the row again. Delegation ids
    are unique across profiles, so this clears the offer in every home."""
    from tools.async_delegation import _offered, _orphan_lock
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    event_id = str(evt.get("_delivery_event_id") or "")
    if not delegation_id and not event_id:
        return
    with _orphan_lock:
        _offered.difference_update({key for key in _offered if key[1] in {delegation_id, event_id}})


def _event_delivery(fn, evt: dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        fn(str(evt.get("delegation_id") or ""), claim_id)


def supervisor_inventory(session_ids: list[str]) -> list[dict[str, Any]]:
    """Live delegations owned by this compression lineage, including after gateway restart.

    A chat key is reusable after /new, so it is never an ownership substitute here.
    Registry timestamps are observations, not proof the child is making progress.
    """
    from tools.async_delegation import _DB_LOCK, _json_object, _transaction
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(f"""SELECT delegation_id, state, task_json, dispatched_at, updated_at
            FROM async_delegations WHERE parent_session_id IN ({','.join('?' for _ in session_ids)})
            AND state IN ('queued','admitted','running','stalling','finalizing')""", session_ids).fetchall()
    return [{"kind": "delegation", "id": row[0], "status": row[1],
             "name": str(_json_object(row[2]).get("goal") or "Background delegation")[:240],
             "started_at": row[3], "last_activity_at": row[4]} for row in rows]


def retirement_inventory(session_key: str, session_id: str) -> list[dict[str, Any]]:
    """Live work and undelivered results owned by one exact gateway conversation."""
    from tools.async_delegation import _DB_LOCK, _json_object, _transaction
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, state, task_json FROM async_delegations
            WHERE (origin_session=? OR parent_session_id=?)
              AND (state IN ('queued','admitted','running','stalling','finalizing') OR delivery_state='pending')""",
            (session_key, session_id)).fetchall()
    return [{"delegation_id": row[0], "state": row[1],
             "goal": _json_object(row[2]).get("goal") or "Background delegation"} for row in rows]


def transfer_retirement_owner(delegation_id: str, old_key: str, old_sid: str, new_key: str, new_sid: str) -> None:
    """Move the durable completion route without stopping or respawning its child."""
    from tools.async_delegation import _DB_LOCK, _records, _records_lock, _transaction, time
    with _records_lock, _DB_LOCK, _transaction() as conn:
        row = conn.execute("SELECT origin_session, parent_session_id FROM async_delegations WHERE delegation_id=?",
                           (delegation_id,)).fetchone()
        if row == (new_key, new_sid):
            return
        if row is None or row[0] != old_key or row[1] != old_sid:
            raise RuntimeError("Delegation ownership changed before retirement handoff")
        conn.execute("""UPDATE async_delegations SET origin_session=?, parent_session_id=?, updated_at=?
                        WHERE delegation_id=?""", (new_key, new_sid, time.time(), delegation_id))
        record = _records.get(delegation_id)
        if record is not None:
            record.update(session_key=new_key, parent_session_id=new_sid)


def refresh_retirement_route(evt: dict[str, Any]) -> None:
    """Reconcile snapshots queued before an authoritative ownership transfer."""
    from tools.async_delegation import _DB_LOCK, _transaction
    if evt.get("type") != "async_delegation" or not evt.get("delegation_id"):
        return
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute("SELECT origin_session, parent_session_id FROM async_delegations WHERE delegation_id=?",
                           (evt["delegation_id"],)).fetchone()
    if row and row[0] and row[0] != evt.get("session_key"):
        evt.update(session_key=row[0], parent_session_id=row[1])
        for field in ("platform", "chat_id", "thread_id", "chat_type"):
            evt.pop(field, None)


def get_durable_delegation(delegation_id: str) -> Optional[dict[str, Any]]:
    from tools.async_delegation import (
        _DB_LOCK, _json_object, _transaction, json, redact_child_result, worker_ledger,
    )
    if remote := worker_ledger():
        saved = remote.read(delegation_id)
        if saved is not None and saved.get("result") is not None:
            return {**saved, "result": redact_child_result(saved["result"])}
        return saved
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute("""SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id, task_json
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchone()
    task = _json_object(row[8]) if row else {}
    return None if row is None else {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1], "dispatched_at": row[2],
        "completed_at": row[3], "result": redact_child_result(json.loads(row[4])) if row[4] else None, "delivery_state": row[5],
        "delivery_attempts": row[6], "origin_session_id": row[7] or "",
        **{key: task[key] if isinstance(task.get(key), dict) else {}
           for key in ("child_execution", "unit_execution")}}


def _json_object(raw: Optional[str]) -> dict[str, Any]:
    from tools.async_delegation import json
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def failed_delegations_for_session(
    origin_ui_session_id: str = "", parent_session_id: str = "", *, limit: int = 20, now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Recently failed async delegation tasks owned by a session, newest first.

    The live roster forgets a child once it ends and does not survive a renderer reload, so a failed
    delegation had nowhere to show (#97202). This reads the durable row instead: one entry per failed
    task (a batch unit that "completed" can still carry failed tasks) with ``delegation_id``,
    ``task_index``, ``goal``, ``status``, ``error``, ``dispatched_at`` and ``completed_at``. Either selector claims a row:
    the UI session id at dispatch, or the spawner's durable session id (survives a reload re-mint)."""
    from tools.async_delegation import (
        _DB_LOCK, _FAILED_TASK_STATES, _FAILURE_SURFACE_WINDOW_S, _json_object, _transaction,
        redact_child_result, time,
    )
    selectors = [(col, val) for col, val in (
        ("origin_ui_session_id", origin_ui_session_id), ("parent_session_id", parent_session_id)) if val]
    if not selectors:
        return []
    cutoff = (now if now is not None else time.time()) - _FAILURE_SURFACE_WINDOW_S
    owner_sql = " OR ".join(f"{col}=?" for col, _ in selectors)
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            f"""SELECT delegation_id, state, dispatched_at, completed_at, task_json, result_json FROM async_delegations
                WHERE ({owner_sql}) AND state NOT IN ('admitted','running','finalizing') AND completed_at >= ?
                ORDER BY completed_at DESC LIMIT ?""",
            (*(val for _, val in selectors), cutoff, limit)).fetchall()
    failed: list[dict[str, Any]] = []
    for delegation_id, state, dispatched_at, completed_at, task_json, result_json in rows:
        task, result = _json_object(task_json), redact_child_result(_json_object(result_json))
        goals = task.get("goals") if isinstance(task.get("goals"), list) and task["goals"] else [task.get("goal") or ""]
        tasks = result["results"] if isinstance(result.get("results"), list) else [] if task.get("is_batch") else [result]
        if not tasks and str(state).lower() in _FAILED_TASK_STATES:
            tasks = [{"task_index": i, "error": result.get("error")} for i in task.get("task_indexes") or [0]]
        for entry in tasks:
            status = str(entry.get("status") or state or "").lower()
            if status not in _FAILED_TASK_STATES:
                continue
            index = entry.get("task_index") if isinstance(entry.get("task_index"), int) else 0
            error = entry.get("error") or result.get("error")
            failed.append({
                "delegation_id": delegation_id, "task_index": index, "status": status,
                # Split units retain the whole call's goals; each result index addresses that list.
                "goal": str((goals[index] if 0 <= index < len(goals) else goals[0]) or ""),
                "error": str(error) if error else None,
                "dispatched_at": dispatched_at, "completed_at": completed_at})
    return failed[:limit]
