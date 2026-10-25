# Session-scoped Fast expiry

Load this unit when changing `agent.fast_expiry_seconds` or the gateway's
session-scoped `/fast fast` / `/fast ultrafast` override lifecycle.

## Required behavior

- `agent.fast_expiry_seconds` defaults to `0`, which disables expiry.
- Only session-scoped static Fast overrides (`fast` / `ultrafast`) expire. The
  bounded `auto` and `cold` modes, explicit `normal`, and `--global` writes do
  not use this deadline.
- The deadline uses `time.time()` and starts when the session override is
  enabled. Repeating the command restarts it.
- Expiry is lazy: the next effective-tier resolution changes the session
  override to explicit normal, not back to the configured default. A live
  running agent filters the expired Fast overlay at wire time; an idle cached
  agent follows the same eviction/reset path as `/fast off`.
- The first user turn after expiry receives one sidecar notice. `/fast status`
  and the picker title report active remaining time in hours/minutes.
- Invalid, boolean, negative, NaN, or infinite values warn and behave as `0`.

## Provenance

Fork patch identity: `session-fast-expiry`.

Parent-directed behavior change: expiry switches the session to normal even when
`agent.service_tier` is configured as `auto` or `fast`, because the requested
meaning is “switch Fast off” rather than “re-read the configured default.”

Upstream NousResearch/hermes-agent issue/PR searches and the upstream source
contained no equivalent session Fast-expiry feature when this patch was made.
Retire this patch if upstream ships equivalent behavior.

## Configuration

The parent can enable the eight-hour policy later with:

```text
hermes config set agent.fast_expiry_seconds 28800
```

Do not run that command as part of source delivery.

## Verification

Run the focused Fast tests, then the repository's scoped regression suite. The
matrix below is the acceptance contract for this patch.

## Design

### Ownership and state transition

`ConversationState.service_tier_override` is authoritative for a session. A
static session selection stores a wall-clock deadline beside that override;
`auto`, `cold`, explicit normal, and global config writes have no deadline.
When `_resolve_session_service_tier` observes `time.time() >= deadline`, it
sets the session override to `None`, clears the deadline, and calls
`_apply_live_service_tier(session_key, None)`. This is exactly the existing
`/fast off` path: an idle cached agent is evicted, while a running agent is
reset without rewriting the request already in flight. The transition happens
once because the deadline is cleared before later resolutions.

The provider/request merge remains upstream-owned. No provider or fallback
writer gains Fast-key ownership. Normal and expiry therefore reuse upstream
agent construction, custom-provider init enrichment, cached-agent eviction,
and running-agent reset behavior. The only new wire metadata is the exact
static-tier mapping produced by `resolve_fast_mode_overrides`:

- `_gateway_fast_expiry_at` is the session deadline, or `0` when inactive.
- `_gateway_session_fast_overlay` is the exact mapping for this turn's static
  tier, or `{}`.

At transport read time, `effective_request_overrides` makes a copy of
`agent.request_overrides`. Once the deadline has passed, it removes only keys
whose current value equals the recorded overlay. It never mutates
`request_overrides` and never touches unrelated keys. Mocks and simple runners
are supported by defensive `getattr`, type checks, and empty defaults. The
same small metadata helper is used when wiring a turn and when `/fast` changes
a live agent.

### Notice lifecycle and ordering

Expiry returns one `transition_notice` through `_resolve_session_service_tier(..., report_transition=True)`; the default call shape remains a tier-only result. `_fast`
commands resolve the session tier first, consume that notice into their reply,
and then apply the requested selection; this makes `/fast fast` and `/fast
status` after a deadline show it exactly once. For an ordinary turn,
`_hmwa_prepare_turn` resolves the tier once immediately before staging
`turn_sidecar_notes`, consumes the notice, and appends it to the ordinary note
batch. `_set_pending_turn_sidecar_notes` and
`_consume_pending_turn_sidecar_notes` remain byte-identical to upstream: a
staged batch is ordinary per-turn state, and expiry does not add another owner.
The later `run_sync` resolution is harmless and silent because the transition
has already cleared its deadline.

If preparation fails after staging and before wiring, the notice can be lost;
that is the accepted limitation and matches the lifecycle of other upstream
sidecar notes. `/background` and other non-`/fast` paths may trigger the
transition silently. A later `/fast status` then reports normal without a
second notice if another path already consumed it.

### Non-goals

- No expiry for CLI, TUI, cron, ACP, subagent, or other non-messaging-gateway
  surfaces.
- No expiry for `auto`, `cold`, explicit normal, or `--global`.
- No changes to `_merge_turn_request_overrides`,
  `_set_pending_turn_sidecar_notes`, `_consume_pending_turn_sidecar_notes`, or
  `_resolve_turn_agent_config`; they remain byte-identical to `origin/main`.
- No provider/fallback rewrite, request-body precedence change, or transport
  capability change.
- No mutation of `agent.request_overrides` from the read-time expiry filter.
- No notice on the enabling turn, no duplicate notice, and no system-prompt or
  transcript injection.

## Test matrix

| # | Scenario | Test coverage |
|---:|---|---|
| 1 | Mid-turn `/fast fast`, turn ends, cached agent, deadline passes, next turn | `test_live_agent_read_time_filter_is_non_mutating_until_resolution`; `test_reused_agent_turn_merges_request_overrides_not_overwrite`; `test_prepare_turn_stages_expiry_notice_once_before_run_sync` |
| 2 | Init-time custom-provider `extra_body` on the next turn after expiry and every normal reuse | `test_reused_agent_turn_merges_request_overrides_not_overwrite` in `tests/gateway/test_custom_provider_request_overrides.py` |
| 3 | Provider-configured top-level `service_tier` preserved on normal turns and after expiry | `test_provider_top_level_service_tier_survives_normal_rewire` |
| 4 | Mid-turn deadline omits only overlay keys, leaves overrides and non-Fast keys unchanged | `test_expired_gateway_fast_overlay_is_filtered_without_mutation`; `test_expired_gateway_fast_overlay_does_not_remove_nonmatching_values` |
| 5 | First turn after expiry gets exactly one notice, next turn none | `test_first_turn_after_expiry_gets_one_notice` |
| 6 | Expiry notice plus another staged note appears once in order | `test_expiry_notice_and_other_staged_note_are_delivered_once` |
| 7 | `/fast fast` after deadline replies with notice once, enables Fast with a new deadline, next turn has no notice | `test_fast_selection_after_expiry_replies_once_and_restarts_deadline` |
| 8 | `/fast status` after deadline shows normal plus notice once | `test_fast_status_after_expiry_shows_normal_and_notice_once` |
| 9 | `ultrafast` follows the same expiry semantics | `test_fast_reenable_restarts_expiry_clock_and_ultrafast_expires` |
| 10 | `auto`, `cold`, `normal`, and `--global` never expire | `test_non_static_fast_modes_never_get_a_deadline`; `test_fast_global_never_expires` |
| 11 | Default `0` never expires | `test_fast_expiry_default_zero_never_expires` |
| 12 | Invalid values warn and count as `0` | `test_invalid_fast_expiry_is_disabled_with_warning` |
| 13 | Status and picker remaining time use h/m | `test_fast_status_reports_remaining_time_in_hours_and_minutes`; `test_fast_picker_title_includes_human_readable_expiry` |
| 14 | Running-agent transition matches `/fast off` | `test_live_agent_read_time_filter_is_non_mutating_until_resolution` |

Tests use frozen virtual clocks; no sleeps are used. Rows 1, 2, 4, and 5
exercise the production ordering through preparation, tier resolution, cache
selection, and wire-time reads rather than adding another Fast-state writer.
