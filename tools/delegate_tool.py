#!/usr/bin/env python3
"""
Delegate Tool -- Subagent Architecture

Spawns child AIAgent instances with a fresh conversation, their own task_id
(terminal session, file-ops cache), the parent's toolsets minus child-blocked
tools, and a focused system prompt built from goal + context. Single-task and
batch (parallel) modes; top-level model calls run in the background while
orchestrator children wait for their own workers. The parent only ever sees
the delegation call and the summary result, never the child's intermediate
tool calls or reasoning.
"""

import json
import logging
import os
import threading
import time
import uuid
import weakref
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.terminal_tool import set_approval_callback as _set_subagent_approval_cb  # noqa: F401  (used via _ChildRun.await_child)
from utils import is_truthy_value

logger = logging.getLogger(__name__)

# The delegate_tool_* siblings hold the pieces split out of this module; every name callers or patching tests reach as
# ``tools.delegate_tool.<name>`` is re-imported here. Mutable flag globals live only in their owning module.
from tools.delegate_tool_child_run import (  # noqa: F401
    _ChildRun, _attach_child, _build_result_entry, _dump_subagent_timeout_diagnostic, _fabricated_entry,
    _lease_child_credential, _merge_late_steer, _register_child, _start_heartbeat, _validate_child_output_schema,
)
from tools.delegate_tool_config import (  # noqa: F401
    _DEFAULT_MAX_CONCURRENT_CHILDREN, _get_child_timeout, _get_max_async_children, _get_max_concurrent_children,
    _get_max_spawn_depth, _get_orchestrator_enabled, _get_subagent_approval_callback, _get_worktree_isolation,
    _inherit_parent_capabilities, _merge_request_overrides, _resolve_child_credential_pool,
    _resolve_child_runtime, _resolve_delegation_credentials, _subagent_auto_approve, _subagent_auto_deny,
)
from tools.delegate_tool_dispatch import _Batch, _announce_batch, _capture_origin, _run_batch
from tools.delegate_tool_progress import (  # noqa: F401
    DelegateEvent, SUBAGENT_FAILURE_STATUSES, _batch_prefix, _build_child_progress_callback,
    _build_child_system_prompt, _clean_error_text, _emit_parent_console, _quiet, _resolve_workspace_hint,
    _safe_progress, format_batch_tag, format_subagent_failure_line,
)
from tools.delegate_tool_registry import (  # noqa: F401
    _CONTROL_ACTIONS, _active_subagents, _active_subagents_lock, _capture_gateway_steer_authority,
    _handle_control_action, _is_descendant_of, _owns_subagent_record, _register_subagent, _unregister_subagent,
    get_subagent_attribution, interrupt_subagent, is_spawn_paused, list_active_subagents, set_spawn_paused,
    steer_subagent,
)
from tools.delegate_tool_tasks import _coerce_task_schemas, _normalize_task_list
from tools.delegate_tool_toolsets import (  # noqa: F401
    DELEGATE_BLOCKED_TOOLS, _expand_parent_toolsets, _resolve_child_toolsets, _strip_blocked_tools,
)
from tools.delegate_tool_results import (  # noqa: F401
    _apply_summary_budget, _build_child_preserving_parent_tools, _run_child_lifecycle, _summarize_tool_arguments,
)

_ROLES = frozenset({"leaf", "orchestrator"})

# Nested delegation is granted by depth/role in _build_child_agent, never by the
# model naming toolsets (there is no model-facing toolsets argument).
def _normalize_role(r: Optional[str]) -> str:
    """'leaf' | 'orchestrator'; None/empty/unknown -> 'leaf' (unknown warns)."""
    r_norm = str(r).strip().lower() if r else "leaf"
    if r_norm not in _ROLES:
        logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
        return "leaf"
    return r_norm

DEFAULT_MAX_ITERATIONS = 250
_HEARTBEAT_INTERVAL = 30  # seconds between parent activity heartbeats during delegation
# Stale-heartbeat thresholds (cycles of _HEARTBEAT_INTERVAL with no progress). Progress = iteration, current_tool OR
# last_activity_ts advancing; an in-flight model wait refreshes last_activity_ts, so slow models are not "idle". Idle
# stays tight so a truly wedged child doesn't mask the gateway timeout; in-tool is much higher so legitimately long
# tools can finish.
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 450s idle between turns → stale
_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 1200s stuck on same tool → stale

def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _open_child_session_db(parent_agent) -> Any:
    """DEDICATED SessionDB handle for the child, or None: the parent's handle can be closed by its own lifecycle while
    a background child still flushes (transcript silently dropped). It MUST open the same db FILE as the parent's
    handle (non-launch profiles), else lineage / session_search break; released by the child's close() via
    _owns_session_db."""
    # Each child gets a DEDICATED SessionDB connection instead of the parent's live object. The parent's
    # handle is owned by the parent's lifecycle (cron run_job's finally block, gateway session end, /new)
    # and can be closed while a fire-and-forget background child is still flushing on a daemon thread —
    # every subsequent flush then hits the closed handle and the child's transcript is silently dropped
    # (#81267). It MUST point at the same database FILE as the parent's handle: parents can hold non-default
    # per-profile handles (tui_gateway opens SessionDB(db_path=<profile>/ state.db) for non-launch
    # profiles), and a bare SessionDB() would write the child's transcript into the launch profile's db,
    # breaking parent_session_id lineage and session_search. AsyncSessionDB wrappers (gateway) forward
    # .db_path via __getattr__, so this works through them.
    parent_session_db = getattr(parent_agent, "_session_db", None)
    if parent_session_db is None:
        return None
    with _quiet("subagent: failed to open dedicated SessionDB; child persistence disabled", exc_info=True):
        from hermes_state_registry import acquire
        _parent_db_path = getattr(parent_session_db, "db_path", None)
        if _parent_db_path is None:
            return acquire()
        # Only a REAL path may be followed. A parent whose handle is a stand-in (tests, embedding
        # apps) exposes a non-path ``db_path``; materializing that would create a database at a
        # synthetic location instead of leaving the child without persistence.
        if isinstance(_parent_db_path, (str, Path)):
            return acquire(Path(_parent_db_path))
        return None
    return None


def _apply_child_cache_ttl(child) -> None:
    """A delegated child never uses the 1h cache tier. The tier is priced for a person who steps
    away between turns (2x write vs 1.25x for 5m, #14971); a subagent calls every few seconds for
    minutes and is gone, so it pays the 2x on every tool result and never collects the retention.
    Caching itself stays exactly as configured (disabled stays disabled)."""
    if getattr(child, "_cache_ttl", None) == "1h":
        child._cache_ttl = "5m"


def _seed_resumed_launch_metadata(child, launch_metadata: dict) -> None:
    """Keep validated launch authority available to compressed continuation rows."""
    metadata = deepcopy(launch_metadata)
    child._session_init_model_config["_delegation_launch"] = metadata
    setattr(child, "_delegation_launch_metadata", metadata)


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    task_count: int,
    parent_agent,
    # Credential overrides from delegation config
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    override_request_overrides: Optional[Dict[str, Any]] = None,
    override_max_tokens: Optional[int] = None,
    # ACP transport overrides from trusted delegation config.
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
    # Configuration block that owns this route's fallback policy. Internal
    # callers such as /review pass auxiliary.review here.
    routing_cfg: Optional[Dict[str, Any]] = None,
    # Trusted internal capability contract (currently used by native review).
    child_tool_policy: Optional[str] = None,
    # Legacy; accepted for wire compat but ignored (capability is depth-derived).
    role: str = "leaf",
    # HERMES-108: a trusted named definition from ``delegation.subagents``.
    subagent_definition=None,
    resolved_reasoning=None,
    resolved_fallback_routes=(),
    moa_snapshot=None,
    resume_session_id=None,
    resume_workspace_path=None,
    resume_launch_metadata=None,
    resume_claim_id=None,
    resume_credential_pool=None,
    resume_credential_id=None,
):
    """Build (don't run) a child AIAgent on the main thread. override_* (from delegation config) replace parent
    inheritance so children can run on a different provider:model pair."""
    import uuid as _uuid
    from run_agent import AIAgent
    from agent.delegation_context import delegated_child_context
    # Role is depth-derived: a child may delegate iff the kill switch is on and
    # depth budget remains below max_spawn_depth. The `role` arg is ignored.
    child_depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    max_spawn = _get_max_spawn_depth()
    effective_role = "orchestrator" if _get_orchestrator_enabled() and child_depth < max_spawn else "leaf"

    # One subagent_id shared by the progress callback, spawn_requested event and
    # the live registry; parent_id is set when THIS parent is itself a subagent.
    subagent_id = f"sa-{task_index}-{_uuid.uuid4().hex[:8]}"
    parent_subagent_id = getattr(parent_agent, "_subagent_id", None)

    delegation_cfg = _load_config()
    inspection_only = child_tool_policy == "inspection_only"
    if inspection_only:
        from agent.review_policy import INSPECTION_TOOL_NAMES
        parent_names = set(getattr(parent_agent, "valid_tool_names", None) or ())
        missing = sorted(INSPECTION_TOOL_NAMES - parent_names)
        if missing:
            raise ValueError(
                "Inspection-only review requires parent access to: " + ", ".join(missing)
            )
    # Build from the parent's normal snapshot, then apply_review_tool_policy freezes
    # the advertised and executable surface to the exact inspection allow-list.
    # Asking _resolve_child_toolsets for the synthetic review-inspection toolset
    # would be intersected away unless the parent explicitly enabled that name.
    requested_toolsets = None if inspection_only else toolsets
    child_toolsets, child_disabled_toolsets = _resolve_child_toolsets(
        parent_agent,
        requested_toolsets,
        effective_role,
        inherit_mcp_toolsets=not inspection_only,
    )

    child_prompt = _build_child_system_prompt(
        goal, context,
        workspace_path=resume_workspace_path or _resolve_workspace_hint(parent_agent),
        role=effective_role,
        max_spawn_depth=max_spawn, child_depth=child_depth,
    )
    if subagent_definition is not None:
        child_prompt += ("\n\n## Named Subagent Instructions\n"
                         "Follow these within governing safety, permissions, and task scope.\n"
                         + subagent_definition.instructions)
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # Shared ref: session_id once the child exists, delegation_id once
    # delegate_task stamps it — both ride on every relayed event.
    child_session_ref: Dict[str, Any] = {}
    child_progress_cb = _build_child_progress_callback(
        task_index, goal, parent_agent, task_count, subagent_id=subagent_id, parent_id=parent_subagent_id,
        depth=max(0, child_depth - 1),  # 0 = first-level child for the UI
        model=model or getattr(parent_agent, "model", None), toolsets=child_toolsets, session_ref=child_session_ref,
    )
    rt = _resolve_child_runtime(
        parent_agent, delegation_cfg, parent_api_key, model=model, override_provider=override_provider,
        override_base_url=override_base_url, override_api_key=override_api_key, override_api_mode=override_api_mode,
        override_max_tokens=override_max_tokens, override_acp_command=override_acp_command,
        override_acp_args=override_acp_args,
        routing_cfg=routing_cfg,
    )
    if override_request_overrides is not None:
        # honored whenever set, incl. the inherit branch where
        # _resolve_delegation_credentials already merged OVER the parent's
        request_overrides = dict(override_request_overrides)
    else:
        request_overrides = {} if override_provider else dict(getattr(parent_agent, "request_overrides", {}) or {})
    parent_sid = getattr(parent_agent, "session_id", None)
    child_session_db = _open_child_session_db(parent_agent)
    child_optional_kwargs: Dict[str, Any] = {}
    if subagent_definition is not None:
        # A named child reads shared knowledge but never writes it, and cannot manage skills.
        child_optional_kwargs["memory_access_mode"] = "read_only"
        child_disabled_toolsets = [*(child_disabled_toolsets or []), "skill_management"]
    with delegated_child_context(read_only_knowledge=subagent_definition is not None):
        try:
            child = AIAgent(
                **rt, max_iterations=max_iterations, prefill_messages=getattr(parent_agent, "prefill_messages", None),
                enabled_toolsets=child_toolsets, disabled_toolsets=child_disabled_toolsets, quiet_mode=True,
                ephemeral_system_prompt=child_prompt, log_prefix=f"[subagent-{task_index}]", platform="subagent",
                skip_context_files=True, skip_memory=True, clarify_callback=None,
                thinking_callback=(
                    (lambda text: _safe_progress(child_progress_cb, "_thinking", text) if text else None)
                    if child_progress_cb else None
                ),
                session_db=child_session_db, parent_session_id=parent_sid,
                session_id=resume_session_id, request_overrides=request_overrides,
                tool_progress_callback=child_progress_cb,
                iteration_budget=None,  # fresh budget per subagent
                **child_optional_kwargs,
            )
        except BaseException:
            # No child close() will ever run: release the dedicated handle here.
            if child_session_db is not None:
                with _quiet(None):
                    from hermes_state_registry import release_or_close
                    release_or_close(child_session_db)
            raise
    child._print_fn = getattr(parent_agent, "_print_fn", None)
    from agent.review_policy import remove_parent_only_review_tools
    remove_parent_only_review_tools(child)
    if child_tool_policy is not None:
        from agent.review_policy import apply_review_tool_policy
        apply_review_tool_policy(child, child_tool_policy)
    _apply_child_cache_ttl(child)
    if child_session_db is not None:
        child._owns_session_db = True  # released by the child's close(), never by the parent
    # Ownership transfer for the dedicated handle: the child's close() must release it (nothing else holds a
    # reference), and no parent teardown can close it out from under a background child (#81267).
    child_session_ref["session_id"] = getattr(child, "session_id", "") or ""
    child._progress_identity_ref = child_session_ref
    child._delegate_depth, child._delegate_role = child_depth, effective_role  # post-degrade role
    child._subagent_id, child._parent_subagent_id = subagent_id, parent_subagent_id
    # Ownership chain for action=list/steer/stop; weakref so a finished parent
    # can be collected while a detached child record lingers in the registry.
    try:
        child._delegate_parent_ref = weakref.ref(parent_agent)
    except TypeError:
        child._delegate_parent_ref = None  # non-weakref-able test doubles
    # Sidebar marker: subagent sessions stay out of session pickers even when a
    # parent delete orphans them (mirrors /branch's ``_branched_from``).
    if parent_sid and getattr(child, "_session_init_model_config", None) is not None:
        child._session_init_model_config["_delegate_from"] = parent_sid
    # Shared pool lets children rotate credentials on rate limits.
    if subagent_definition is not None:
        from tools.custom_subagents import (
            RuntimePin, _authority_mapping_fingerprint, _nonsecret_mapping,
            inherited_credential_pool, nonsecret_route_url,
        )
        child._delegation_named_type = subagent_definition.name
        if child.provider == "moa":
            child._moa_preset_snapshot = moa_snapshot
            from agent.moa_loop import build_moa_facade
            child.client = build_moa_facade(child, child.model)
        child_pool = resume_credential_pool or inherited_credential_pool(child, parent_agent, delegation_cfg)
        child._credential_pool = child_pool
        if child.provider != "moa":
            child._delegation_fallback_routes = tuple(resolved_fallback_routes)
            child._fallback_chain = [route.native_entry() for route in resolved_fallback_routes]
            child._fallback_index = 0
            child._delegation_runtime_pin = RuntimePin.from_child(child, subagent_definition, resolved_reasoning)
        if getattr(child, "_session_init_model_config", None) is not None and not resume_session_id:
            parent_root = parent_sid
            if child_session_db is not None and parent_sid:
                with _quiet(None):
                    parent_root = child_session_db.get_compression_lineage(parent_sid)[0]
            launch = {
                "version": 1,
                "subagent_type": subagent_definition.name,
                "description": subagent_definition.description,
                "instructions": subagent_definition.instructions,
                "parent_session_root": parent_root,
                "provider": child.provider, "model": child.model,
                "base_url": nonsecret_route_url(child.base_url), "api_mode": child.api_mode,
                "reasoning_effort": getattr(getattr(child, "_delegation_runtime_pin", None), "reasoning_effort", None),
                "authority_fingerprint": getattr(getattr(child, "_delegation_runtime_pin", None), "_credential_digest", None),
                "request_overrides": _nonsecret_mapping(json.loads(getattr(
                    getattr(child, "_delegation_runtime_pin", None), "request_overrides_json", "{}"
                ))),
                "request_overrides_fingerprint": _authority_mapping_fingerprint(json.loads(getattr(
                    getattr(child, "_delegation_runtime_pin", None), "request_overrides_json", "{}"
                ))),
                "fallbacks": [route.metadata() for route in resolved_fallback_routes],
                "enabled_toolsets": list(child_toolsets or []),
            }
            if moa_snapshot is not None:
                launch["moa"] = moa_snapshot.metadata()
            if child_pool is not None and callable(getattr(child_pool, "entry_id_for_api_key", None)):
                credential_id = child_pool.entry_id_for_api_key(getattr(child, "api_key", None))
                if credential_id:
                    launch["credential_pool_entry_id"] = credential_id
            child._session_init_model_config["_delegation_launch"] = launch
            setattr(child, "_delegation_launch_metadata", launch)
        if resume_session_id:
            if not isinstance(resume_launch_metadata, dict):
                raise ValueError("resumed delegated child is missing validated launch metadata")
            _seed_resumed_launch_metadata(child, resume_launch_metadata)
            child._delegation_resume_needs_reload = True
            child._delegation_resume_fail_if_busy = True
            setattr(child, "_delegation_resume_claim_id", resume_claim_id)
            setattr(child, "_delegation_resume_admitted", False)
            credential_id = resume_credential_id or resume_launch_metadata.get("credential_pool_entry_id")
            if credential_id:
                setattr(child, "_delegation_resume_credential_id", credential_id)
            setattr(child, "_delegation_resume_workspace_path", resume_workspace_path)
    else:
        child_pool = _resolve_child_credential_pool(rt["provider"], parent_agent, rt["base_url"])
        if child_pool is not None:
            child._credential_pool = child_pool

    _attach_child(parent_agent, child)  # interrupt propagation
    # spawn_requested now — the child may queue for seconds when the pool is
    # saturated — then the subagent_start lifecycle hook.
    _safe_progress(child_progress_cb, "subagent.spawn_requested", preview=goal)
    with _quiet("subagent_start hook invocation failed", exc_info=True):
        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
        _invoke_hook(
            "subagent_start", parent_session_id=parent_sid,
            parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "", parent_subagent_id=parent_subagent_id,
            child_session_id=getattr(child, "session_id", None), child_subagent_id=subagent_id,
            child_role=effective_role, child_goal=goal,
        )
    return child

def _resume_history_is_safe(messages: Any) -> bool:
    """True when every persisted tool request has a matching result.

    A missing result is an unknown external-effect boundary: resuming could make
    the model repeat an already-started action, so parent continuation fails
    closed instead of replaying it.
    """
    if not isinstance(messages, list):
        return False
    pending = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role == "assistant" and message.get("tool_calls"):
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                return False
            for call in calls:
                if not isinstance(call, dict):
                    return False
                call_id = call.get("call_id") or call.get("id")
                if not isinstance(call_id, str) or not call_id.strip():
                    return False
                pending.add(call_id.split("|", 1)[0].strip())
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id.strip():
                pending.discard(tool_call_id.split("|", 1)[0].strip())
    return not pending


def _refresh_resumable_launch_metadata(child, launch_metadata: dict) -> dict:
    """Record the active pool account without putting its credential in durable state."""
    updated = deepcopy(launch_metadata)
    credential_id = getattr(child, "_credential_pool_entry_id", None)
    if not isinstance(credential_id, str) or not credential_id:
        return updated
    active = (getattr(child, "provider", None), getattr(child, "model", None))
    if active == (updated.get("provider"), updated.get("model")):
        updated["credential_pool_entry_id"] = credential_id
        updated["authority_fingerprint"] = __import__("hashlib").sha256(
            str(getattr(child, "api_key", "") or "").encode()
        ).hexdigest()
        return updated
    for fallback in updated.get("fallbacks") or []:
        if isinstance(fallback, dict) and active == (fallback.get("provider"), fallback.get("model")):
            fallback["credential_pool_entry_id"] = credential_id
            fallback["authority_fingerprint"] = __import__("hashlib").sha256(
                str(getattr(child, "api_key", "") or "").encode()
            ).hexdigest()
            break
    return updated


def _restore_fallback_authority(routes, expected, normalize_route_base_url):
    """Rebind frozen fallbacks to their persisted stable pool account IDs."""
    if not isinstance(expected, list) or len(routes) != len(expected):
        return routes
    restored = []
    for route, metadata in zip(routes, expected):
        credential_id = metadata.get("credential_pool_entry_id") if isinstance(metadata, dict) else None
        if not credential_id:
            restored.append(route)
            continue
        pool = route._credential_pool
        entries = pool.entries() if pool is not None and callable(getattr(pool, "entries", None)) else ()
        entry = next((item for item in entries if getattr(item, "id", None) == credential_id), None)
        api_key = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None)
        entry_provider = str(getattr(entry, "provider", None) or route.provider)
        entry_base = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None) or route.base_url
        if (
            entry is None or not api_key or entry_provider != route.provider
            or normalize_route_base_url(str(entry_base)) != normalize_route_base_url(route.base_url)
        ):
            raise ValueError("delegated child fallback stable credential can no longer be authorized")
        restored.append(replace(
            route, api_key=str(api_key),
            credential_digest=__import__("hashlib").sha256(str(api_key).encode()).hexdigest(),
            credential_pool_entry_id=str(credential_id),
        ))
    return tuple(restored)


def _fallback_metadata_matches(routes, expected) -> bool:
    """Compare public route data plus complete override authority, including legacy-safe metadata."""
    from tools.custom_subagents import _authority_mapping_matches
    if not isinstance(expected, list) or len(routes) != len(expected):
        return False
    for route, stored in zip(routes, expected):
        if not isinstance(stored, dict):
            return False
        overrides = json.loads(route.request_overrides_json)
        if not _authority_mapping_matches(
            overrides, stored.get("request_overrides") or {},
            stored.get("request_overrides_fingerprint"),
        ):
            return False
        current = route.metadata()
        if "request_overrides_fingerprint" not in stored:
            current.pop("request_overrides_fingerprint", None)
        if "credential_pool_entry_id" not in stored:
            current.pop("credential_pool_entry_id", None)
        if current != stored:
            return False
    return True


def _run_single_child(
    task_index: int, goal: str, child=None, parent_agent=None, *, owner_session_id: Optional[str] = None,
    owner_transport: Any = None, owner_session_record: Any = None, **_kwargs,
) -> Dict[str, Any]:
    """Run a pre-built child agent (called from a worker thread) and return its result entry.

    Contract, derived from the child's structured completion fields:
      status      ∈ {completed, budget_exhausted, interrupted, failed} —
                    budget exhaustion is a parent-resumable segment boundary,
                    never task completion; a structured failure (failed=True /
                    non-empty error) or invalid terminal state is "failed".
      exit_reason ∈ {completed, max_iterations, interrupted, error} —
                    "max_iterations" only for genuine budget exhaustion
                    (completed=False with no failure fields), never for errors.
      truncated   == (exit_reason == "max_iterations").

    * ``"completed"``        — normal task finish. See #97655.
    * ``"budget_exhausted"`` — safe checkpoint; only an explicit parent resume
      starts another configured segment.
    """
    child_progress_cb = getattr(child, "tool_progress_callback", None)
    child_pool, leased_cred_id = _lease_child_credential(child)
    # Heartbeat keeps the parent's _last_activity_ts moving so the gateway inactivity timeout doesn't fire while the
    # child works; it stops itself once the child looks stale (see _HEARTBEAT_STALE_CYCLES_*).
    heartbeat = _start_heartbeat(child, parent_agent, task_index)
    # TUI/RPC registry entry (kill/pause/status by subagent_id); None for test
    # doubles without a stable id. Unregistered in the finally block.
    _subagent_id = _register_child(
        child, parent_agent, goal, owner_session_id=owner_session_id, owner_transport=owner_transport,
        owner_session_record=owner_session_record,
    )
    run = _ChildRun(child, parent_agent, task_index, goal, _subagent_id, child_progress_cb)
    # Set when a timed-out Future still owns the child: closing it from this
    # thread before the worker settles races the conversation's finally path.
    _child_close_deferred = False
    try:
        heartbeat.start()
        _safe_progress(child_progress_cb, "subagent.start", preview=goal)
        run.seed_workspace()
        result, failure_entry, _child_close_deferred = run.await_child()
        if failure_entry is not None:
            return failure_entry

        schema = _validate_child_output_schema(child, result, task_index, run.child_task_id, run.relay_text)
        _merge_late_steer(result, _subagent_id, child)
        # Flush any remaining batched progress to gateway
        if child_progress_cb and hasattr(child_progress_cb, "_flush"):
            with _quiet("Progress callback flush failed: %s"):
                child_progress_cb._flush()

        duration = run.elapsed()
        entry = _build_result_entry(child, result, task_index, duration, schema)
        if entry.get("status") in {"completed", "budget_exhausted"}:
            db = getattr(child, "_session_db", None)
            named_child = getattr(child, "_delegation_named_type", None) is not None
            if named_child:
                entry["resume_available"] = False
            safe_history = _resume_history_is_safe((result or {}).get("messages"))
            if not safe_history:
                entry["resume_blocked_reason"] = "unresolved_tool_effects"
            if db is not None and safe_history:
                try:
                    launch_metadata = deepcopy(getattr(child, "_delegation_launch_metadata", None))
                    if isinstance(launch_metadata, dict):
                        launch_metadata = _refresh_resumable_launch_metadata(child, launch_metadata)
                    model_config_patch = {
                        "_delegation_completed": True,
                        "_delegation_outcome": entry["status"],
                        "_delegation_resume_claimed_at": None,
                        "_delegation_active_route": {
                            "provider": getattr(child, "provider", None),
                            "model": getattr(child, "model", None),
                        },
                    }
                    if isinstance(launch_metadata, dict):
                        model_config_patch["_delegation_launch"] = launch_metadata
                    db.patch_session_model_config(
                        getattr(child, "session_id", ""), model_config_patch,
                    )
                    if named_child:
                        entry["resume_available"] = True
                except Exception as exc:
                    logger.warning("Could not mark delegated child resumable: %s", exc, exc_info=True)
                    entry["resume_error"] = "durable continuation marker could not be persisted"
        run.append_sibling_write_reminder(entry)
        run.emit_complete(result, entry, duration)
        return run.attach_worktree(entry)
    except Exception as exc:
        # Close steer acceptance before any completion callback (see _merge_late_steer).
        _late_pending_steer = run.close_steering()
        logging.exception(f"[subagent-{task_index}] failed")
        # Entry status "error" (contract), progress event status "failed" (UI vocabulary).
        return run.finish_failed(
            _fabricated_entry(task_index, "error", str(exc), child, run.elapsed()), _late_pending_steer,
            preview=str(exc), summary=str(exc), status="failed",
        )
    finally:
        with _quiet("Could not restore unadmitted delegated resume grant: %s"):
            _restore_unadmitted_resume_grant(child)
        run.cleanup(heartbeat=heartbeat, child_pool=child_pool, leased_cred_id=leased_cred_id, close_deferred=_child_close_deferred)


def _parse_model_config(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _resolve_resume_launch(task, definitions, parent_agent):
    """Restore one completed child from durable, nonsecret launch metadata."""
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.route_identity import normalize_route_base_url
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import parse_reasoning_effort
    from tools.custom_subagents import (
        FallbackDefinition, ResolvedSubagentLaunch, SubagentDefinition,
        freeze_fallback_routes, nonsecret_route_url, _authority_mapping_matches,
    )

    requested = task.get("resume_session_id")
    if not isinstance(requested, str) or not requested.strip():
        raise ValueError("resume_session_id must be a nonempty delegated child session id")
    db = getattr(parent_agent, "_session_db", None)
    if db is None:
        raise ValueError("child resume requires the parent's durable session database")
    tip = db.resolve_resume_session_id(requested.strip())
    row = db.get_session(tip) if tip else None
    if not row:
        raise ValueError("resume_session_id does not name an existing session")
    row_profile = row.get("profile_name")
    if row_profile and row_profile != get_active_profile_name():
        raise ValueError("resume_session_id belongs to another Hermes profile")
    config = _parse_model_config(row.get("model_config"))
    launch = config.get("_delegation_launch")
    if not isinstance(launch, dict) or launch.get("version") != 1:
        raise ValueError("resume_session_id is not a resumable delegated child")
    if not config.get("_delegation_completed"):
        raise ValueError("delegated child has no verified resumable checkpoint")
    role = launch.get("subagent_type")
    if not isinstance(role, str) or role not in definitions:
        raise ValueError("delegated child references an unknown configured role")
    if task.get("subagent_type") not in (None, role):
        raise ValueError("requested subagent_type conflicts with the delegated child")
    requested_preset = task.get("moa_preset")
    stored_preset = (launch.get("moa") or {}).get("preset") if isinstance(launch.get("moa"), dict) else None
    if requested_preset is not None and requested_preset != stored_preset:
        raise ValueError("requested moa_preset conflicts with the delegated child")
    parent_sid = getattr(parent_agent, "session_id", None)
    parent_lineage = db.get_compression_lineage(parent_sid) if parent_sid else []
    parent_root = parent_lineage[0] if parent_lineage else parent_sid
    if not parent_root or launch.get("parent_session_root") != parent_root:
        raise ValueError("resume_session_id is foreign to the requesting parent lineage")
    delegated_from = config.get("_delegate_from")
    delegated_lineage = db.get_compression_lineage(delegated_from) if delegated_from else []
    delegated_root = delegated_lineage[0] if delegated_lineage else delegated_from
    if not delegated_root or delegated_root != parent_root:
        raise ValueError("resume_session_id is not a delegated child of the requesting parent lineage")

    provider, model = str(launch.get("provider") or ""), str(launch.get("model") or "")
    effort = launch.get("reasoning_effort")
    definition = SubagentDefinition(
        name=role, description=str(launch.get("description") or ""),
        instructions=str(launch.get("instructions") or ""), provider=provider,
        model=model, reasoning_effort=effort,
        fallbacks=tuple(FallbackDefinition(
            provider=str(item.get("provider") or ""), model=str(item.get("model") or ""),
            reasoning_effort=item.get("reasoning_effort"),
        ) for item in launch.get("fallbacks") or [] if isinstance(item, dict)),
    )
    snapshot = None
    resume_credential_pool = None
    resume_credential_id = None
    if provider == "moa":
        from agent.moa_loop import restore_moa_preset
        snapshot = restore_moa_preset(launch.get("moa") or {})
        # Reuse the same virtual transport contract as a fresh named MoA child;
        # physical authority remains inside the restored frozen snapshot.
        creds = {
            "provider": "moa", "model": model, "base_url": "moa://local",
            "api_key": None, "api_mode": "chat_completions",
            "request_overrides": None, "max_output_tokens": None,
        }
        reasoning = None
        fallbacks = ()
    else:
        creds = resolve_runtime_provider(requested=provider, target_model=model)
        stable_credential_id = launch.get("credential_pool_entry_id")
        if stable_credential_id is not None:
            pool = _resolve_child_credential_pool(provider, parent_agent, creds.get("base_url"))
            entries = pool.entries() if pool is not None and callable(getattr(pool, "entries", None)) else []
            entry = next((item for item in entries if getattr(item, "id", None) == stable_credential_id), None)
            if entry is None:
                raise ValueError("delegated child stable credential identity is no longer authorized")
            entry_provider = str(getattr(entry, "provider", "") or provider)
            entry_base = getattr(entry, "runtime_base_url", None) or creds.get("base_url")
            if (
                entry_provider != provider
                or normalize_route_base_url(nonsecret_route_url(str(entry_base or "")))
                   != normalize_route_base_url(str(launch.get("base_url") or ""))
            ):
                raise ValueError("delegated child stable credential identity changed route authority")
            runtime_key = getattr(entry, "runtime_api_key", None)
            if not isinstance(runtime_key, str) or not runtime_key:
                raise ValueError("delegated child stable credential identity has no usable runtime credential")
            creds = {**creds, "api_key": runtime_key, "base_url": entry_base}
        authority_matches = (
            stable_credential_id is not None
            or __import__("hashlib").sha256(str(creds.get("api_key") or "").encode()).hexdigest()
               == str(launch.get("authority_fingerprint") or "")
        )
        if (
            creds.get("provider") != provider
            or (creds.get("model") or model) != model
            or str(creds.get("api_mode") or "") != str(launch.get("api_mode") or "")
            or normalize_route_base_url(nonsecret_route_url(str(creds.get("base_url") or "")))
               != normalize_route_base_url(str(launch.get("base_url") or ""))
            or not authority_matches
            or not _authority_mapping_matches(
                creds.get("request_overrides") or {}, launch.get("request_overrides") or {},
                launch.get("request_overrides_fingerprint"),
            )
        ):
            raise ValueError("delegated child primary route can no longer be authorized exactly")
        reasoning = parse_reasoning_effort(effort) if effort is not None else None
        fallbacks = freeze_fallback_routes(
            definition, primary_provider=provider, primary_model=model
        )
        expected_fallbacks = launch.get("fallbacks") or []
        fallbacks = _restore_fallback_authority(fallbacks, expected_fallbacks, normalize_route_base_url)
        if not _fallback_metadata_matches(fallbacks, expected_fallbacks):
            raise ValueError("delegated child fallback routes no longer match their frozen identities")
        resume_credential_pool = None
        resume_credential_id = stable_credential_id
        active = config.get("_delegation_active_route") or {"provider": provider, "model": model}
        active_id = (active.get("provider"), active.get("model")) if isinstance(active, dict) else (None, None)
        if active_id != (provider, model):
            active_index = next((index for index, route in enumerate(fallbacks)
                                 if (route.provider, route.model) == active_id), None)
            if active_index is None:
                raise ValueError("delegated child active route is not in its frozen fallback chain")
            active_route = fallbacks[active_index]
            creds = active_route.native_entry()
            resume_credential_pool = active_route._credential_pool
            resume_credential_id = active_route.credential_pool_entry_id
            reasoning = parse_reasoning_effort(active_route.reasoning_effort) if active_route.reasoning_effort is not None else None
            fallbacks = fallbacks[active_index + 1:]
            definition = SubagentDefinition(
                name=role, description=definition.description, instructions=definition.instructions,
                provider=active_route.provider, model=active_route.model,
                reasoning_effort=active_route.reasoning_effort,
                fallbacks=tuple(FallbackDefinition(route.provider, route.model, route.reasoning_effort)
                                for route in fallbacks),
            )
    return ResolvedSubagentLaunch(
        definition, creds, reasoning, fallbacks, snapshot, tip,
        tuple(str(x) for x in launch.get("enabled_toolsets") or ()),
        workspace_path=str(row.get("cwd")) if row.get("cwd") else None,
        launch_metadata=deepcopy(launch),
        _credential_pool=resume_credential_pool if provider != "moa" else None,
        resume_credential_id=resume_credential_id if provider != "moa" else None,
    )


def _preflight_task_runtime(task_list, cfg, credentials_cfg, parent_agent, legacy_creds):
    """``([(definition, creds, reasoning), ...], None)`` or ``([], error)`` for the WHOLE batch.

    Resolution is a preflight, not a per-child step: an unknown ``subagent_type`` anywhere must fail
    the batch before the first child exists, so a partially-valid batch cannot spawn its valid half.
    """
    from dataclasses import replace
    from tools.custom_subagents import (
        ResolvedSubagentLaunch, freeze_fallback_routes, parse_definitions,
        resolve_definition, resolve_named_credentials,
    )

    # Registry validation is not a per-task failure: report it as the config error it is, naming the
    # invalid definition (item 3). list/steer/stop never consult definitions, so running children
    # stay controllable.
    try:
        definitions = parse_definitions(cfg)
    except ValueError as exc:
        return [], (
            f"Invalid delegation.subagents configuration: {exc}. No named role can be selected until "
            "this is fixed; action=list/steer/stop still control running children."
        )

    task_runtime: List[ResolvedSubagentLaunch] = []
    try:
        for task in task_list:
            if task.get("resume_session_id") is not None:
                if credentials_cfg:
                    raise ValueError("resumed named subagents cannot override credentials_cfg")
                task_runtime.append(_resolve_resume_launch(task, definitions, parent_agent))
                continue
            definition = resolve_definition(definitions, task.get("subagent_type"))
            if definition is not None and credentials_cfg:
                raise ValueError("named subagents cannot override an internal credentials_cfg route")
            if definition is None:
                if task.get("moa_preset") is not None:
                    raise ValueError("moa_preset is only valid for a named MoA subagent")
                task_runtime.append(ResolvedSubagentLaunch(None, legacy_creds, None))
                continue
            selected_preset = task.get("moa_preset")
            if selected_preset is not None:
                if definition.provider != "moa":
                    raise ValueError("moa_preset is only valid for a named MoA subagent")
                if not isinstance(selected_preset, str) or not selected_preset.strip():
                    raise ValueError("moa_preset must be a nonempty preset name")
                allowed = definition.moa_presets or (definition.model,)
                if selected_preset not in allowed:
                    raise ValueError(f"moa_preset {selected_preset!r} is not allowed for subagent_type {definition.name!r}")
                definition = replace(definition, model=selected_preset)
            task_creds, reasoning = resolve_named_credentials(definition, cfg, parent_agent)
            snapshot = task_creds.pop("moa_snapshot", None)
            fallback_routes = freeze_fallback_routes(
                definition,
                primary_provider=str(task_creds.get("provider") or getattr(parent_agent, "provider", "")),
                primary_model=str(task_creds.get("model") or getattr(parent_agent, "model", "")),
            ) if definition.provider != "moa" else ()
            task_runtime.append(ResolvedSubagentLaunch(
                definition, task_creds, reasoning, fallback_routes, snapshot
            ))
    except ValueError as exc:
        return [], f"Task {len(task_runtime)} preflight failed: {exc}"

    resume_ids = [
        launch.resume_session_id for launch in task_runtime
        if launch.resume_session_id is not None
    ]
    if resume_ids:
        db = getattr(parent_agent, "_session_db", None)
        claim_batch = getattr(db, "claim_delegated_resumes", None)
        claim_id = uuid.uuid4().hex
        if not callable(claim_batch) or not claim_batch(resume_ids, claim_id=claim_id):
            return [], (
                "Delegated child resume batch is unsafe, already claimed, or no longer resumable. "
                "No child was started."
            )
        task_runtime = [
            replace(launch, resume_claim_id=claim_id)
            if launch.resume_session_id is not None else launch
            for launch in task_runtime
        ]
    return task_runtime, None


def _creds_overrides(creds: Dict[str, Any]) -> Dict[str, Any]:
    """``_build_child_agent`` route overrides for one resolved credentials dict."""
    return {
        "override_provider": creds["provider"], "override_base_url": creds["base_url"],
        "override_api_key": creds["api_key"], "override_api_mode": creds["api_mode"],
        "override_request_overrides": creds.get("request_overrides"),
        "override_max_tokens": creds.get("max_output_tokens"), "override_acp_command": creds.get("command"),
        "override_acp_args": creds.get("args"),
    }


def _release_resume_launches(parent_agent, launches: List[Any]) -> bool:
    """Compensate one claimed batch when construction failed before admission."""
    claimed = [launch for launch in launches if getattr(launch, "resume_claim_id", None)]
    if not claimed:
        return False
    claim_ids = {launch.resume_claim_id for launch in claimed}
    if len(claim_ids) != 1:
        return False
    release = getattr(getattr(parent_agent, "_session_db", None), "release_delegated_resumes", None)
    return bool(callable(release) and release(
        [launch.resume_session_id for launch in claimed], claim_id=next(iter(claim_ids)),
    ))


def _restore_unadmitted_resume_grant(child) -> bool:
    """Restore one exact claim only when the native turn lease never admitted it."""
    claim_id = getattr(child, "_delegation_resume_claim_id", None)
    if not claim_id or getattr(child, "_delegation_resume_admitted", False):
        return False
    release = getattr(getattr(child, "_session_db", None), "release_delegated_resumes", None)
    session_id = getattr(child, "session_id", None)
    return bool(callable(release) and session_id and release([session_id], claim_id=claim_id))


def _build_children(
    task_list: List[Dict[str, Any]], task_schemas: List[Optional[Dict[str, Any]]], creds: Dict[str, Any], *,
    top_role: str, max_iterations: int, parent_agent, live_deleg_id: Optional[str], live_writers: list,
    task_runtime: Optional[List[Any]] = None,
    routing_cfg: Optional[Dict[str, Any]] = None,
    child_tool_policy: Optional[str] = None,
) -> tuple[List[tuple], Optional[str]]:
    """Build every child on the main thread (construction is not thread-safe);
    ``(children, None)`` or ``([], error)`` on an explicit-pin preflight failure.

    ``task_runtime`` carries the per-task ``(definition, creds, reasoning)`` resolved by the batch
    preflight, so a named child is built on ITS route rather than the batch's first one."""
    from tools.delegation_live_log import wrap_progress_callback
    from tools.delegation_output_schema import append_output_contract
    overrides = _creds_overrides(creds)
    children = []
    for i, t in enumerate(task_list):
        _task_schema = task_schemas[i] if i < len(task_schemas) else None
        _child_context = t.get("context")
        if _task_schema is not None:
            _child_context = append_output_contract(_child_context, _task_schema)
        _launch = task_runtime[i] if task_runtime and i < len(task_runtime) else None
        _definition = _launch.definition if _launch else None
        _task_creds = _launch.credentials if _launch else creds
        _reasoning = _launch.reasoning if _launch else None
        _task_overrides = overrides if _definition is None else _creds_overrides(_task_creds)
        try:
            child = _build_child_preserving_parent_tools(
                task_index=i, goal=t["goal"], context=_child_context,
                toolsets=list(_launch.enabled_toolsets) if _launch and _launch.enabled_toolsets is not None else None,
                model=_task_creds["model"], max_iterations=max_iterations, task_count=len(task_list),
                parent_agent=parent_agent, role=_normalize_role(t.get("role") or top_role),
                subagent_definition=_definition, resolved_reasoning=_reasoning,
                resolved_fallback_routes=_launch.fallback_routes if _launch else (),
                moa_snapshot=_launch.moa_snapshot if _launch else None,
                resume_session_id=_launch.resume_session_id if _launch else None,
                resume_workspace_path=_launch.workspace_path if _launch else None,
                resume_launch_metadata=_launch.launch_metadata if _launch else None,
                resume_claim_id=_launch.resume_claim_id if _launch else None,
                resume_credential_pool=_launch._credential_pool if _launch else None,
                resume_credential_id=_launch.resume_credential_id if _launch else None,
                routing_cfg=routing_cfg,
                child_tool_policy=child_tool_policy,
                **_task_overrides,
            )
        except ValueError as exc:
            _release_resume_launches(parent_agent, task_runtime or [])
            return [], str(exc)
        except BaseException:
            _release_resume_launches(parent_agent, task_runtime or [])
            raise
        if _task_schema is not None:
            with _quiet("Could not attach output schema to child %d", i):
                child._delegate_output_schema = _task_schema
        # Tee progress events into the live transcript (wrapper keeps the
        # _flush contract and swallows writer failures).
        _writer = live_writers[i] if i < len(live_writers) else None
        if _writer is not None:
            child.tool_progress_callback = wrap_progress_callback(getattr(child, "tool_progress_callback", None), _writer)
            child._live_transcript_path = str(_writer.path)
        if live_deleg_id:
            setattr(child, "_delegation_id", live_deleg_id)
            _ident_ref = getattr(child, "_progress_identity_ref", None)
            if isinstance(_ident_ref, dict):
                _ident_ref["delegation_id"] = live_deleg_id
        children.append((i, t, child))
    return children, None


def delegate_task(
    goal: Optional[str] = None, context: Optional[str] = None, tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None, role: Optional[str] = None, background: Optional[bool] = None,
    output_schema: Optional[Dict[str, Any]] = None, action: Optional[str] = None, subagent_id: Optional[str] = None,
    message: Optional[str] = None, parent_agent=None, credentials_cfg: Optional[Dict[str, Any]] = None,
    child_tool_policy: Optional[str] = None,
    completion_contract: Optional[Dict[str, Any]] = None,
) -> str:
    """Spawn child agents (single ``goal`` or ``tasks=[...]`` batch) or control running ones. ``action``
    list/steer/stop run synchronously and bypass the pause gate, depth limit and async dispatch. ``role`` is legacy
    (per-task beats top-level; capability is depth-derived). Returns JSON with one results entry per task, or a
    dispatch handle when running in the background."""
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    normalized_action = (action or "").strip().lower()
    if normalized_action in _CONTROL_ACTIONS:
        return _handle_control_action(normalized_action, subagent_id, message, parent_agent)
    if normalized_action and normalized_action != "spawn":
        return tool_error(f"Unknown action '{action}'. Use spawn (default), list, steer, or stop.")

    # Operator kill switch (TUI / delegation.pause RPC): blocks NEW spawns only.
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    top_role = _normalize_role(role)
    if child_tool_policy not in (None, "legacy_unrestricted", "inspection_only"):
        return tool_error(f"Invalid review tool policy: {child_tool_policy!r}")
    # background applies to single tasks AND batches: a batch is ONE async unit
    # that joins on every child and re-enters as a single consolidated message.
    background = is_truthy_value(background, default=False) if background is not None else False

    depth = getattr(parent_agent, "_delegate_depth", 0)
    max_spawn = _get_max_spawn_depth()
    if depth >= max_spawn:
        return tool_error(
            f"Delegation depth limit reached (depth={depth}, max_spawn_depth={max_spawn}). Raise "
            f"delegation.max_spawn_depth in config.yaml if deeper nesting is required (no hard ceiling, but each level "
            f"multiplies API cost)."
        )

    # Spawning on a config we could not read would silently substitute legacy delegation for the
    # user's configured roles, so this path refuses where the tolerant loader degrades (item 2).
    # Reading the recorded error (rather than calling the strict loader) keeps ``_load_config`` the
    # single read point every caller and test patches.
    cfg = _load_config()
    config_error = last_delegation_config_error()
    if config_error:
        return tool_error(
            f"Delegation configuration could not be loaded: {config_error}. Fix delegation config in "
            "config.yaml and retry; refusing to spawn on legacy defaults that are not what you configured."
        )
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    # Caller-supplied max_iterations is ignored: the config value is authoritative
    # so budgets stay predictable (kwarg kept for internal callers/tests).
    if max_iterations is not None and max_iterations != default_max_iter:
        logger.debug(
            "delegate_task: ignoring caller-supplied max_iterations=%s; using delegation.max_iterations=%s from config",
            max_iterations, default_max_iter,
        )
    # credentials_cfg (internal callers only, e.g. /review → auxiliary.review) is
    # a per-call override shaped like the delegation config section.
    try:
        creds = _resolve_delegation_credentials(credentials_cfg if credentials_cfg else cfg, parent_agent)
    except ValueError as exc:
        # Explicit-pin preflight failures (e.g. pinned delegation.command missing from PATH) refuse the
        # spawn loudly (#80450).
        return tool_error(str(exc))
    max_children = _get_max_concurrent_children()
    task_list, err = _normalize_task_list(goal, context, tasks, output_schema, top_role, max_children)
    if not err:
        task_schemas, err = _coerce_task_schemas(task_list, output_schema)
    if err:
        return tool_error(err)

    # HERMES-108: resolve every task's named definition BEFORE constructing ANY child. A batch with
    # one bad subagent_type must not leave a valid sibling already spawned and running.
    task_runtime, err = _preflight_task_runtime(task_list, cfg, credentials_cfg, parent_agent, creds)
    if err:
        return tool_error(err)
    creds = dict(task_runtime[0].credentials)

    overall_start = time.monotonic()
    # Live transcripts: cache/delegation/live/<id>/task-<n>.log per task, a side channel with zero effect on message
    # content or prompt caching. Best-effort: on failure live_paths is empty and delegation proceeds.
    from tools.delegation_live_log import create_live_transcripts
    live_deleg_id, live_writers, live_paths = create_live_transcripts(
        task_list, context, model=creds.get("model"), provider=creds.get("provider"),
        routing=_task_routing_metadata(task_runtime, parent_agent),
    )
    _announce_batch(parent_agent, len(task_list), live_deleg_id)
    origin = _capture_origin()

    children, err = _build_children(
        task_list, task_schemas, creds, top_role=top_role, max_iterations=default_max_iter, parent_agent=parent_agent,
        live_deleg_id=live_deleg_id, live_writers=live_writers, task_runtime=task_runtime, routing_cfg=credentials_cfg, child_tool_policy=child_tool_policy,
    )
    if err:
        return tool_error(err)
    batch = _Batch(
        task_list, children, parent_agent, creds, context, top_role, max_children,
        live_deleg_id, live_writers, live_paths, *origin, overall_start,
        completion_contract=completion_contract,
    )
    return _run_batch(batch, background)


def _task_routing_metadata(task_runtime: list, parent_agent=None) -> list:
    """Per-child resolved role/provider/model/effort for batch telemetry.

    Derived from the SAME tuples the children are launched from, so mixed batches record what each
    child actually got instead of inheriting task 0's route. Nonsecret by construction: credentials
    never enter this dict. An unnamed (legacy) child that inherits the parent's route resolves to no
    explicit creds; the inherited values ARE its resolved route, so they are recorded rather than
    left blank."""
    from agent.reasoning_effort import requested_effort

    inherited_model = getattr(parent_agent, "model", None)
    inherited_provider = getattr(parent_agent, "provider", None)
    inherited_effort = requested_effort(getattr(parent_agent, "reasoning_config", None))

    routing = []
    for launch in task_runtime:
        if hasattr(launch, "definition"):
            definition, task_creds, reasoning = launch.definition, launch.credentials, launch.reasoning
        else:
            definition, task_creds, reasoning = launch
        creds = task_creds or {}
        if reasoning and reasoning.get("enabled") is False:
            effort = "none"
        elif reasoning:
            effort = requested_effort(reasoning)
        else:
            effort = inherited_effort
        routing.append({
            "subagent_type": definition.name if definition is not None else None,
            "provider": creds.get("provider") or inherited_provider,
            "model": creds.get("model") or inherited_model,
            "reasoning_effort": effort,
        })
    return routing


class DelegationConfigError(RuntimeError):
    """Delegation configuration exists but could not be loaded.

    Distinct from an absent ``delegation`` block, which is a legitimate, silent default. Silently
    degrading a *failed* load to legacy settings was the bug: a user with ``delegation.subagents``
    configured would get unnamed legacy delegation with no signal that their config never loaded."""

# Thread-local, not module-global: the gateway runs many sessions in one process, and one thread's
# broken read must not refuse another thread's spawn.
_delegation_config_state = threading.local()

def _record_delegation_config_error(exc: BaseException | None) -> None:
    _delegation_config_state.error = None if exc is None else str(exc)

def last_delegation_config_error() -> str | None:
    """Loader failure seen by THIS thread's most recent ``_load_config()``."""
    return getattr(_delegation_config_state, "error", None)

def _delegation_block(full, *, source: str) -> dict:
    """Extract and validate the ``delegation`` block of a loaded config."""
    if not isinstance(full, dict):
        raise DelegationConfigError(f"{source} must be a mapping, got {type(full).__name__}")
    cfg = full.get("delegation")
    if cfg is not None and not isinstance(cfg, dict):
        raise DelegationConfigError(f"delegation must be a mapping, got {type(cfg).__name__}")
    _record_delegation_config_error(None)
    return cfg or {}

def load_delegation_config() -> dict:
    """The delegation config, raising on a genuine loader failure.

    Returns ``{}`` only when the config loaded fine and simply has no ``delegation`` block. Any other
    outcome — loader exception, non-mapping ``delegation`` value — raises :class:`DelegationConfigError`."""
    if os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1":
        # ``--ignore-user-config`` means "run on defaults". A broken legacy CLI_CONFIG in that mode
        # degrades to {} exactly as it always did: refusing to spawn there would take plain unnamed
        # delegation away from a user who configured no roles at all. The loud failure belongs to the
        # real config path below, the one that can hide configured roles.
        _record_delegation_config_error(None)
        return _legacy_delegation_config()
    try:
        from hermes_cli.config import load_config_readonly
        full = load_config_readonly()
    except Exception as exc:
        raise DelegationConfigError(f"could not load Hermes config: {type(exc).__name__}: {exc}") from exc
    return _delegation_block(full, source="Hermes config")

def _legacy_delegation_config() -> dict:
    """Legacy ``cli.CLI_CONFIG`` delegation block, or ``{}``. Best-effort only: reached exclusively
    from the tolerant ``_load_config()`` path after the strict loader recorded why it failed."""
    try:
        from cli import CLI_CONFIG
        cfg = CLI_CONFIG.get("delegation") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}

def _load_config() -> dict:
    """The ``delegation`` config section (read-only — do NOT mutate).

    Tolerant by contract: never raises, so schema rebuilds and limit lookups survive a broken config.
    Callers that must not run on a half-understood configuration — anything that SPAWNS — call
    ``load_delegation_config()`` and surface :class:`DelegationConfigError`, or read the recorded
    failure via ``last_delegation_config_error()``. This stays the single read point every caller and
    test patches, so it keeps its name and identity."""
    try:
        return load_delegation_config()
    except DelegationConfigError as exc:
        # Tolerant callers keep working best-effort, but the failure is no longer invisible: it is
        # recorded so the schema and the spawn path report it instead of quietly serving legacy
        # settings as if they were the user's.
        _record_delegation_config_error(exc)
        return _legacy_delegation_config()

# ── OpenAI function-calling schema ──────────────────────────────────────────

def _build_top_level_description(definition_error: str | None = None, load_error: str | None = None) -> str:
    """delegate_task description: ONLY guidance stated nowhere else in the schema
    (limits live in the 'tasks' parameter description, rebuilt per get_definitions())."""
    try:
        orchestration_available = _get_max_spawn_depth() >= 2 and _get_orchestrator_enabled()
    except Exception:
        orchestration_available = False
    # Mention recursion only where it's actually available. send_message is deliberately not named (gateway-internal
    # vocabulary); model_tools session-filters the list to tools the session has.
    if orchestration_available:
        restrictions_rule = (
            "- Children cannot call clarify, memory, or cronjob.\n"
            f"- Children can themselves delegate while depth remains (max_spawn_depth={_get_max_spawn_depth()}); the "
            "runtime derives this from depth automatically.\n"
        )
    else:
        restrictions_rule = "- Children cannot call delegate_task, clarify, memory, or cronjob.\n"
    # Configuration diagnostics lead: a parent that cannot see WHY a role vanished will silently fall
    # back to unnamed delegation and never mention it. `action` (list/steer/stop) keeps working
    # regardless — those paths never read definitions, so existing children stay controllable.
    diagnostics = ""
    if load_error:
        diagnostics += (
            f"CONFIG WARNING: delegation configuration could not be loaded ({load_error}). Spawning is "
            "refused until it loads; list/steer/stop still work on running children.\n\n"
        )
    if definition_error:
        diagnostics += (
            "CONFIG WARNING: a configured subagent role is invalid and no named role is selectable right "
            f"now ({definition_error}). Report this to the user instead of silently delegating unnamed; "
            "list/steer/stop still work on running children.\n\n"
        )
    return diagnostics + _DESCRIPTION_HEAD + restrictions_rule + _DESCRIPTION_TAIL

_DESCRIPTION_HEAD = (
    "Spawn subagents in isolated contexts; each gets its own conversation, terminal session, and toolset, and only its "
    "final summary returns to you. Pass every task in `tasks` — one entry spawns one subagent, several run in parallel "
    "(limit in the tasks description).\n\n"
    "Runs in the background: dispatch returns immediately with live transcript paths, and the completed result (one "
    "consolidated message, results in task order) re-enters the conversation on its own. Do NOT wait or poll; continue "
    "other work. While children run, `action` (list/steer/stop) controls them live — steer when a transcript shows a "
    "child drifting.\n\n"
    "USE FOR: reasoning-heavy subtasks, work that would flood your context with intermediate data, or independent "
    "parallel workstreams.\n"
    "DO NOT USE FOR (use these instead):\n"
    "- Mechanical multi-step work with no reasoning needed -> execute_code\n"
    "- A single tool call -> call the tool directly\n"
    "- Tasks needing user interaction -> subagents cannot ask questions\n"
    "- Durable work that must survive this session -> cronjob or terminal(background=True, notify=True); /stop, /new, "
    "or process exit discards running subagents.\n\n"
    "RULES:\n"
    "- Children know nothing of this conversation: pass everything needed via 'context', including any required "
    "output language, tone, or style (e.g. \"respond in Chinese\").\n"
    "- Child summaries are SELF-REPORTS, not verified facts: a child claiming \"uploaded successfully\" or "
    "\"file written\" may be wrong. For external side effects (uploads, remote writes, publishing), require a "
    "verifiable handle (URL, ID, absolute path) and verify it yourself before telling the user the operation "
    "succeeded.\n"
)
_DESCRIPTION_TAIL = (
    "- Children inherit the parent model unless pinned via delegation.provider / delegation.model in config.yaml, or "
    "unless the task names a `subagent_type` role, whose configured provider/model/effort override those global "
    "defaults.\n"
    "- Selection: prefer a named role whose purpose matches the work; omit `subagent_type` for ordinary delegation "
    "(unchanged legacy behavior); keep simple work with yourself. There is no keyword router — this is your judgment "
    "call, informed by the role descriptions."
)

def _build_tasks_param_description() -> str:
    """Compose the 'tasks' parameter description with current concurrency limit."""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"The task(s), up to {max_children} in parallel for this user (set "
        "via delegation.max_concurrent_children). Each entry spawns one "
        "subagent with isolated context and terminal session; a single task "
        "is a one-entry array. Required when spawning."
    )

def _build_dynamic_schema_overrides() -> dict:
    """Per-call schema overrides (ToolEntry.dynamic_schema_overrides): every
    get_definitions() pass rewrites the descriptions to the user's actual limits."""
    overrides_params = {**DELEGATE_TASK_SCHEMA["parameters"]}
    # Copy properties so the static schema dict is never mutated.
    overrides_params["properties"] = {k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()}
    overrides_params["properties"]["tasks"]["description"] = _build_tasks_param_description()

    # HERMES-108: advertise ONLY the configured aliases and their selection descriptions. The
    # definitions' ``instructions`` are trusted server-side text and never enter the schema.
    from copy import deepcopy
    from tools.custom_subagents import advertised_settings, parse_definitions

    cfg = _load_config()
    load_error = last_delegation_config_error()
    definition_error: str | None = None
    try:
        definitions = parse_definitions(cfg)
    except ValueError as exc:
        # Silently dropping the selector left the parent unable to see that a role it was told about
        # is broken — and unable to tell that apart from "no roles configured". list/steer/stop stay
        # untouched (they never consult definitions); say exactly what is invalid.
        definitions = {}
        definition_error = str(exc)
    if definitions:
        task_schema = deepcopy(overrides_params["properties"]["tasks"])
        task_schema["items"]["properties"]["subagent_type"] = {
            "type": "string",
            "enum": sorted(definitions),
            "description": _build_subagent_type_description(
                [advertised_settings(definitions[name], cfg) for name in sorted(definitions)]),
        }
        overrides_params["properties"]["tasks"] = task_schema

    return {
        "description": _build_top_level_description(definition_error=definition_error, load_error=load_error),
        "parameters": overrides_params,
    }

def _build_subagent_type_description(roles: list) -> str:
    """Advertise each configured role's purpose AND its fixed settings.

    The parent previously saw only a name and a free-text description, so it could not reason about
    cost/latency (which model? which effort?) or know that its own delegation defaults do not apply
    to a named role."""
    lines = [
        "Optional trusted subagent role, defined in config.yaml under delegation.subagents. "
        "Configuration — not this call — fixes each role's provider, model, and reasoning effort; "
        "you choose only the identifier.",
    ]
    for role in roles:
        pinned = "fixed" if role["pinned"] else "inherited"
        lines.append(
            f"- {role['name']} -> {role['model']} / {role['reasoning_effort']} effort ({pinned}): {role['description']}")
    lines.append(
        "Omit subagent_type for ordinary delegation on the global delegation.model / "
        "delegation.reasoning_effort defaults. A named role's settings override those defaults; "
        "nothing you pass here can. Prefer a named role when the work matches its purpose, and keep "
        "simple work with yourself rather than delegating it at all.")
    return "\n".join(lines)

def _p(type_: str, description: str, **extra) -> dict:
    return {"type": type_, **extra, "description": description}

DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # description / tasks.description are placeholders: the real text is built per get_definitions() call by
    # _build_dynamic_schema_overrides() so the model sees the user's actual max_concurrent_children / max_spawn_depth.
    # Lazy (not at import) so cli.CLI_CONFIG isn't forced to load before the test conftest redirects HERMES_HOME.
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # The handler also accepts the legacy single-goal shape (top-level `goal`/`context`/`output_schema`),
            # wrapped into a one-entry batch at dispatch, and a per-task `role` (legacy, ignored: capability is
            # depth-derived). Both unadvertised on purpose (old transcripts only); do not re-add. No maxItems — the
            # runtime limit (delegation.max_concurrent_children) is enforced with a clear error in delegate_task().
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": _p(
                            "string",
                            "What this subagent should accomplish. Be specific and self-contained — it knows "
                            "nothing about your conversation history.",
                        ),
                        "context": _p(
                            "string",
                            "Background THIS child needs: file paths, error messages, constraints. Each child "
                            "sees only its own context — repeat shared background in every task that needs it.",
                        ),
                        "resume_session_id": _p(
                            "string",
                            "Stable child_session_id from a completed or budget-exhausted delegation. Continues that exact "
                            "named child's durable session and frozen route; omit subagent_type or repeat the same role.",
                        ),
                        "moa_preset": _p(
                            "string",
                            "Optional native MoA preset for a named provider: moa role. The preset must be in "
                            "that role's configured moa_presets allowlist; omission uses the role default.",
                        ),
                        "output_schema": _p(
                            "object",
                            "Optional JSON Schema this child's final answer must validate against (told to the "
                            "child up front; parent validates with one bounded correction retry; result gains "
                            "schema_valid, plus schema_errors on failure). Keep it forgiving — require only "
                            "fields you will read.",
                        ),
                    },
                    "required": ["goal"],
                },
                "description": "(rebuilt at get_definitions() time)",
            },
            # `background` (bool) is also accepted — DEPRECATED, ignored: top-level
            # delegations always run in the background. Unadvertised; do not re-add.
            "action": _p(
                "string",
                "Default 'spawn'. Live control of running children: "
                "'list' = ids/goals/status/transcripts; 'steer' = queue "
                "course-correction text into one child (subagent_id + "
                "message) without stopping it; 'stop' = end one child "
                "early (subagent_id; partial result still returns). "
                "Control actions return immediately; goal/tasks are ignored unless spawning.",
                enum=["spawn", "list", "steer", "stop"],
            ),
            "subagent_id": _p("string", "Target for action='steer'/'stop' (ids from the spawn response or action='list')."),
            "message": _p(
                "string",
                "For action='steer': the course correction, appended to "
                "the child's next tool result mid-run. Be directive and specific.",
            ),
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

def _model_background_value(args: dict, parent_agent=None) -> bool:
    """Background flag for the MODEL-facing dispatch path (registry fallback). Top-level delegations always run in the
    background — the model does not choose — for single tasks and fan-out batches alike (one async unit, one
    consolidated result); an orchestrator subagent (depth > 0) is the exception since it needs its workers' results
    within its own turn. The live path is ``run_agent._dispatch_delegate_task``; this mirrors it for the rare case
    the intercept is bypassed. Direct Python callers keep the synchronous default."""
    return not getattr(parent_agent, "_delegate_depth", 0) > 0

_MODEL_HIDDEN_TASK_FIELDS = {"acp_command", "acp_args"}

def _strip_model_hidden_task_fields(tasks: Any) -> Any:
    """Drop trusted-config-only task fields from model-supplied tasks (same list object back when nothing changed)."""
    if not isinstance(tasks, list) or not any(isinstance(t, dict) and _MODEL_HIDDEN_TASK_FIELDS & t.keys() for t in tasks):
        return tasks
    return [{k: v for k, v in t.items() if k not in _MODEL_HIDDEN_TASK_FIELDS} if isinstance(t, dict) else t for t in tasks]


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"), context=args.get("context"), tasks=_strip_model_hidden_task_fields(args.get("tasks")),
        max_iterations=args.get("max_iterations"), role=args.get("role"),
        background=_model_background_value(args, kw.get("parent_agent")), output_schema=args.get("output_schema"),
        action=args.get("action"), subagent_id=args.get("subagent_id"), message=args.get("message"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from concurrent.futures import TimeoutError as FuturesTimeoutError  # noqa: F401,E402
import contextvars  # noqa: F401,E402
import enum  # noqa: F401,E402
import json  # noqa: F401,E402
import os  # noqa: F401,E402
import re  # noqa: F401,E402
import threading  # noqa: F401,E402
from urllib.parse import urlsplit  # noqa: F401,E402
from urllib.parse import urlunsplit  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_CHILD_TIMEOUT': ('tools.delegate_tool_config', 'DEFAULT_CHILD_TIMEOUT'),
    'DEFAULT_MAX_SUMMARY_CHARS': ('tools.delegate_tool_results', 'DEFAULT_MAX_SUMMARY_CHARS'),
    'DEFAULT_TOOLSETS': ('tools.delegate_tool_toolsets', 'DEFAULT_TOOLSETS'),
    'MAX_DEPTH': ('tools.delegate_tool_config', 'MAX_DEPTH'),
    'TOOLSETS': ('toolsets', 'TOOLSETS'),
    'base_url_hostname': ('utils', 'base_url_hostname'),
    'file_state': ('tools', 'file_state'),
    'request_hard_interrupt': ('agent.interrupt_compat', 'request_hard_interrupt'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
