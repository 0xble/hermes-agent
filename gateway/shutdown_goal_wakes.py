"""Identify regenerable goal wakes without discarding absorbed human follow-ups."""

import logging
import math

logger = logging.getLogger("gateway.run")


def retire_regenerable_goal_continuation(session_key, data):
    """Decide once before route/admission; never require a chat notice for a wake."""
    from gateway.shutdown_flush import has_user_content

    if (not session_key or not has_user_content(data)
            or not regenerable_goal_continuation(data) or not _ensure_durable_owner(data)):
        return False
    # Polling owns the next goal wake. Replaying duplicates it; waiting for a
    # successful turn or a user-facing notice leaves a suppressed wake immortal.
    logger.info("Restart recovery outcome: session=%s outcome=%s",
                session_key, "skipped:superseded-goal-continuation")
    return True


def goal_continuation_only(event):
    """Persist provenance while process-local synthetic/merge capabilities still exist."""
    from gateway.session_synthetic import project_synthetic

    pending, seen = [event], set()
    while pending:
        candidate = pending.pop()
        if id(candidate) in seen:
            continue  # merge trails flatten transitive descendants as well as direct children
        seen.add(id(candidate))
        projection = project_synthetic(candidate)
        if (not projection or not isinstance(projection.get("metadata"), dict)
                or projection["metadata"].get("goal_continuation") is not True
                or getattr(candidate, "reply_expected", None) is not False):
            return False
        absorbed = getattr(candidate, "_native_absorbed_synthetic", [])
        if not isinstance(absorbed, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("metadata"), dict)
                or item["metadata"].get("goal_continuation") is not True for item in absorbed):
            return False
        pending.extend(getattr(candidate, "_hermes_absorbed_events", ()))
    return True


def goal_wake_identities(event) -> list[dict]:
    """Preserve every merged wake's supersession fence, not just the head's."""
    pending, seen, identities = [event], set(), []
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        metadata = getattr(item, "metadata", None) or {}
        projections = [metadata, *(row.get("metadata") or {} for row in
                                  getattr(item, "_native_absorbed_synthetic", ()) or ())]
        for row in projections:
            identities.append({key: row.get(key) for key in (
                "goal_continuation_session_id", "goal_continuation_instance",
                "goal_continuation_created_at",
            )})
        pending.extend(getattr(item, "_hermes_absorbed_events", ()) or ())
    return identities


def _goal_wake_superseded(state, metadata):
    """An active, unparked goal may depend on its one outstanding continuation."""
    if state is None or state.status != "active":
        return True
    instance = metadata.get("goal_continuation_instance")
    if instance is None:
        return False
    if instance != state.created_at:
        return True
    queued_at = metadata.get("goal_continuation_created_at")
    # A later evaluated turn owns the successor continuation or persisted wait.
    # Age alone is never sufficient: only parked goals have a polling backstop.
    return (isinstance(queued_at, (int, float)) and not isinstance(queued_at, bool)
            and math.isfinite(queued_at) and queued_at > 0
            and state.last_turn_at > queued_at)


def _wake_identities(data):
    return data.get("goal_continuation_wakes") or [data["metadata"]]


def _all_wakes_superseded(state, data):
    sid = data["metadata"]["goal_continuation_session_id"]
    identities = _wake_identities(data)
    return isinstance(identities, list) and all(
        isinstance(row, dict) and row.get("goal_continuation_session_id") == sid
        and _goal_wake_superseded(state, row) for row in identities
    )


def _ensure_durable_owner(data):
    """Atomically re-arm the idle poller before deleting an active goal's last spool."""
    import time
    import uuid
    from hermes_cli.goals import GoalState, _get_session_db

    db = _get_session_db()
    if db is None:
        return False
    key = "goal:" + data["metadata"]["goal_continuation_session_id"]

    def transaction(conn):
        from hermes_cli.session_controls import compression_successor

        target_key = key
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (target_key,)).fetchone()
        state = GoalState.from_json(row[0]) if row else None
        if state is None or state.status == "cleared":
            successor = compression_successor(key[len("goal:"):], cursor=conn)
            if successor:
                target_key = "goal:" + successor
                row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (target_key,)).fetchone()
                if not row:
                    return False  # The migration may still be in progress.
                state = GoalState.from_json(row[0])
            elif conn.execute("SELECT 1 FROM sessions WHERE parent_session_id = ? LIMIT 1",
                              (key[len("goal:"):],)).fetchone():
                return False  # Ambiguous/forked lineage is not deletion authority.
        if not _all_wakes_superseded(state, data):
            return False
        if state is not None and state.status == "active" and not (
                state.waiting_on_pid is not None or state.waiting_on_session is not None
                or state.waiting_on_user_message or state.waiting_until):
            # A later evaluator saved its decision before enqueue. A crash there can
            # lose the successor; retain a durable poller owner, never replay this spool.
            now = time.time()
            state.waiting_until = state.waiting_since = now
            state.waiting_reason = "Resume goal after superseded shutdown wake"
            state.mutation_id = uuid.uuid4().hex
            conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (state.to_json(), target_key))
        return True

    try:
        return db._execute_write(transaction)
    except Exception:
        logger.warning("Could not persist pending goal recovery owner", exc_info=True)
        return False


def _legacy_prompt_matches(data: dict, state) -> bool:
    """Require the entire formatter output, independent of current autonomy settings."""
    from hermes_cli.goals import (
        CONTINUATION_PROMPT_TEMPLATE, CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
        CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE, _render_extra_criteria,
    )
    from hermes_cli.goals_autonomy import AUTONOMY_CONTINUATION_TEMPLATE

    contract = state.contract.render_block()
    if state.has_contract():
        if state.subgoals:
            contract = f"{contract}\n{_render_extra_criteria(state.subgoals)}"
        plain = CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE.format(goal=state.goal, contract_block=contract)
    elif state.subgoals:
        plain = CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE.format(
            goal=state.goal, subgoals_block=state.render_subgoals_block())
    else:
        plain = CONTINUATION_PROMPT_TEMPLATE.format(goal=state.goal)
    return data.get("text") in (
        plain,
        AUTONOMY_CONTINUATION_TEMPLATE.format(
            goal=state.goal, contract_block=state.contract.render_block(),
            subgoals_block=state.render_subgoals_block(),
        ),
    )


def regenerable_goal_continuation(data: dict) -> bool:
    """Only a proved, human-free and superseded goal wake can be retired.

    A later goal turn owns the replacement wake or wait; a current unparked
    goal may still need this continuation. Legacy spools did not persist merge
    provenance: require an exact generated prompt match, not a prefix or a quiet
    reply contract, which can also describe human input.
    """
    metadata = data.get("metadata")
    if (not isinstance(metadata, dict) or metadata.get("goal_continuation") is not True
            or data.get("reply_expected") is not False or data.get("message_id")
            or data.get("media_urls") or data.get("media")):
        return False
    if data.get("goal_continuation_only") is False:
        return False
    sid = metadata.get("goal_continuation_session_id")
    if not isinstance(sid, str) or not sid:
        return False
    try:
        from hermes_cli.goals import _get_session_db, load_goal

        db = _get_session_db()
        if db is None:
            return False  # load_goal also returns None for an unavailable store, not just absence.
        state = load_goal(sid, db=db)
        proven = data.get("goal_continuation_only") is True
        if not proven:
            proven = (state is not None
                      and metadata.get("goal_continuation_instance") == state.created_at
                      and _legacy_prompt_matches(data, state))
        return proven and _all_wakes_superseded(state, data)
    except Exception:
        # A missing definition or unavailable profile store is not deletion authority.
        logger.debug("Legacy pending goal wake identity unavailable for %s", sid, exc_info=True)
        return False
