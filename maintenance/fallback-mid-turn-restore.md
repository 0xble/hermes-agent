# Mid-turn fallback restore

Owns the `fallback-mid-turn-restore` fork patch. It adopts the narrow upstream
fix from [NousResearch/hermes-agent#88628](https://github.com/NousResearch/hermes-agent/pull/88628),
commit `aa4e600a8e7bd33687c657c9f6edc38cc46159e5`, on the fork's release-based
main. The conversation loop reuses `restore_primary_runtime` before each next
model request, so its shared cross-process cooldown record remains authoritative:
an active record prevents a mid-turn primary probe, while a record cleared by a
successful probe in another session lets this session restore at its next call.

The request-local system prompt is synchronized after restoration. The restore
check runs at the start of the iteration-entry phase and does not alter the
messages list, so assistant tool calls remain paired with their tool results. The optional,
provider-independent per-entry `max_stay_seconds` also retries only fallback
entries strictly earlier than the current entry while the shared primary
cooldown still blocks a restore. If none is available, the current route stays
active and its timer resets; stay expiry never demotes it or emits a failure
notice or failure metric.
A concrete regression against the upstream-only port kept the last entry active
for the next tool-loop request even though the first fallback was available;
primary recovery alone cannot cover this case. Unset limits retain the normal
cooldown-gated primary restoration without an extra fallback-chain walk.

The check runs only for real provider fallbacks, never for temporary voice or
explicit model routes. It skips a request rebuild that is still retrying a
just-activated fallback. Mid-turn primary probes require a local cooldown
deadline or a held shared cooldown record: the normal restore path reads the
cross-process record and honors an active outage, expiry, or clearance elsewhere.
A generic-500/transport primary that never armed a cooldown has no such deadline,
so it gets a local one (`agent/fallback_route_recovery_uncooled.py`): the first
re-check is 60 seconds after the fallback, and a primary that fails again soon
after a re-check doubles the spacing up to 15 minutes. Without it the episode
lasted until the next user turn, and a delegated child is a single turn of
hundreds of requests, so it never left its spare. The re-check is per agent and
in memory: it arms no cooldown and writes no shared record, and the restore
still runs through `restore_primary_runtime`, so an outage another process
recorded still blocks it. A deterministic refusal (content policy, missing
model, entitlement) gets no re-check and stays turn-scoped. An expired
`max_stay_seconds` may still retry earlier fallback entries from index zero,
and retries that uncooled primary only at its re-check. Runtime re-evaluation failures roll back
the route and reset the per-entry timer instead of disrupting the turn.

## Proof and retirement

The proof surface is `tests/agent/test_mid_turn_primary_restore.py`, including
prompt provenance, active shared-cooldown blocking, cross-session shared-record
clearance, ordered-chain recovery after a failed primary retry, stay-limit
re-evaluation, unchanged behavior for unset limits, and rollback on errors.
Real conversation-loop regressions also cover voice routing across a tool call,
uncooled generic-500 failures across several tool iterations, the bounded
uncooled re-check (return to a recovered primary, wider spacing after a relapse,
one probe per window, none for a deterministic refusal), an uncooled
stay-limit fallback re-walk, non-last-entry stay limits without demotion,
quiet earlier-backup reclamation, and bookkeeping reset on restore or explicit
switch. Voice turns that start on a provider fallback remain pinned across a
tool call even when another session clears the shared outage. Both accepted
and rejected voice routes preserve the session fallback's entry and stay timer.
A reloaded session comes back on its pre-fallback route (#152333):
`tests/agent/test_fallback_session_reload.py` covers the marker, legacy gateway
rows, and that every explicit-pick writer (`update_session_model`, a local
policy model selection, the TUI live persist) clears both the marker and the
legacy `gateway_runtime.fallback_active` flag through `clear_fallback_markers`.
Retire this patch when the selected upstream release contains equivalent
mid-turn restore and optional stay-limit behavior and the regressions pass
without the local implementation.

Fork patch identities: `fallback-mid-turn-restore`.

Contract tests: `tests/agent/test_mid_turn_primary_restore.py`,
  `tests/agent/test_fallback_session_reload.py`.

## Companion resilience surfaces

The same identity carries the changes that shipped with the uncooled re-check,
because they share its failure mode (a turn that leaves its primary and cannot
get back, or cannot say why):

- `agent/turn_api_call_admission.py`: pre-send token-per-minute admission. A
  minute bucket is matched on provider, endpoint and model, and the check fails
  open whenever headers or an estimate is missing. Before a predicted overage
  moves the turn, a read-only probe asks whether any untried chain entry could
  take it (not cooled, within the spare cap and budget). With none, the request
  is sent on the primary and the chain cursor and cooldown flags are untouched.
- `agent/retry_utils.py`, `agent/nous_rate_guard.py`: a days-long `Retry-After`
  on a per-minute bucket is bounded to the minute-window cap before it is used
  or persisted. The 60 s overage window is armed only when the error text
  names a per-minute limit; a daily limit prints the same accounting line. The persisted Nous wait is also held to the 600 s retry cap;
  only a structured welcome-tier `rate_limited` refusal keeps its stated wait.
- `agent/turn_failure_routes.py`: the failure card names the `fallback_providers`
  list that feeds the failing agent and only the routes that ran out this episode.
- `hermes_cli/fallback_config.py`, `hermes_cli/config_value_checks.py`,
  `hermes_cli/config_migrations_delegation_roles.py`: fallback chains are checked
  for at least three entries across three upstream families. Only a declared
  list is checked: an absent or null chain is skipped and `[]` opts out. The
  checked lists are the model-map options, the top-level list, `cron`,
  `delegation` and the auxiliary chains. The main and cron primaries are read
  from `model.default`. A migration keeps a user-authored model map and a
  declared `delegation.fallback_providers` as written and reports any chain
  below the bar. One resolver, `infer_upstream_family`, names a route's family
  for validation and for the spare guards; it drops a vendor segment
  (`anthropic/claude-x`) before reading the model name. The web model-map save
  runs the same check against the whole config, so alias-named routes are
  expanded. `PUT /api/config` does not run it.
- `tools/delegate_tool_config.py`: a child is always built on its role primary.
  When another process already cooled that primary, construction adopts the
  shared cooldown, so the child starts on the first admissible chain entry
  through the spare guards and returns to the role primary mid-turn.

Proof: `tests/agent/test_model_fallback_resilience.py`,
`tests/agent/test_nous_rate_guard.py` and the delegated-child and relapse
regressions in `tests/agent/test_mid_turn_primary_restore.py`.
