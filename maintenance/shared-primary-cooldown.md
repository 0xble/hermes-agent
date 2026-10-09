# Shared primary cooldown

## Fork patch identity

This maintenance unit owns the fork patch identity `shared-primary-cooldown`.

## Required behavior

A rate-limited, billing-limited, upstream-rate-limited, or overloaded primary route
records its wall-clock cooldown under `$HERMES_HOME/state/model_cooldowns.json`. The
arming set is `_SHARED_COOLDOWN_REASONS` in `agent/fallback_cooldown.py`; the writer,
the outage notice and the chain-exhaustion guard all read it. New agents,
subagents, cron runs, and gateway-created agents adopt the first configured fallback
without retrying the primary while that record is active. The outage notice is
atomically claimed once per outage; the recovery notice is emitted only after a
successful primary response and only by the process that clears the record. A
response counts as primary success only when the agent's live route (provider,
base_url, model) equals the record's route and no fallback is active, so a
fallback reply never clears the outage. A rate-limit switch keeps a generic notice
when the shared record is unavailable or when it moves the user to a model other
than the one the outage notice announced.

An agent already on a provider fallback re-reads the shared record at every turn
start. While the record is active, the agent stays on the fallback and its in-memory
deadline follows the record, so a longer window that another process re-armed wins.
Restore only proceeds once the shared window has passed or the record is gone, and
restore never rewrites the record.

Overload (`FailoverReason.overloaded`: HTTP 529 or 503 unless the body reads as context
overflow, any 5xx or 400 whose body names a local memory ceiling, a 429 with overload
wording, a status-less overload message, Gemini `unavailable`, and 403
`upstream_unavailable`) arms exactly like a rate limit. A `Retry-After` on the overload response is the
provider reset; without one the shared no-reset backoff applies. The record's `reason`
is `overloaded`, so `hermes fallback status` shows it and the outage notice reads
"is overloaded until HH:MM". Generic 500/502 (`server_error`), timeouts and connection
errors do not arm; they keep the per-session retry, the generic fallback notice and the
short chain-exhaustion cooldown. `switch_deferred_by_reset` still applies only to rate
limits.

Readers treat a record whose `reset_at` or `recorded_at` is not a finite number (NaN,
±Infinity, junk) as malformed and prune it, and `arm_cooldown` ignores a non-finite
provider reset. A corrupted state file therefore cannot pin a route forever or make
`hermes fallback status` raise.

A 429 that arrives while the record is still active came from a request already in
flight before the outage was recorded, not from a fresh probe. It keeps the current
backoff level, so a burst of concurrent 429s costs one 60 s window rather than
60 → 960 s. A re-arm without a provider reset never shortens an active window, so a
header-less 429 cannot pull a known two-hour reset forward. A provider reset time
still replaces the window, since it is the authoritative answer. Backoff escalates
only on a 429 after the window lapsed, which is a real re-probe.

An expired record counts as the same outage only within a grace period: the larger of
10 minutes and the record's own window, capped at the 4 h backoff ceiling. Busy
profiles probe within seconds of expiry, so a record nobody re-armed within that grace
belongs to an outage that already ended. A 429 after the grace starts a new outage,
with a fresh outage id, an unclaimed notice, and backoff starting again at 60 s.
Readers prune these stale records, along with malformed ones.

`hermes fallback status` and `hermes fallback cooldowns` list active records.
`hermes fallback cooldowns clear --all` clears every record.
`hermes fallback cooldowns clear <provider/model | model>` clears records that match
exactly. A bare model name matches that model on every provider, and substrings never
match. Neither form changes the configured fallback chain. `hermes fallback clear`
keeps its original meaning and empties the chain. A clear does not interrupt a turn
that is already running. Live cached agents pick it up at their next turn-start check,
which finds no record and lets the agent retry the primary.

## Source surfaces and proof

- `agent/shared_primary_cooldown.py`: file-locked, atomic per-`HERMES_HOME` state.
- `agent/fallback_cooldown.py`: writer and shared backoff escalation.
- `agent/agent_runtime_helpers.py`: fresh-agent adoption, the turn-start shared-record refresh for
  agents already on a fallback, and turn-start gating.
- `agent/chat_completion_helpers.py` and `agent/chat_completion_nonstream.py`: notice claim and recovery clear.
- `hermes_cli/fallback_cmd.py`, `hermes_cli/subcommands/fallback.py`: `status` and `cooldowns [clear]`.
- `tests/agent/test_shared_primary_cooldown.py`: separate-process persistence, one-claim regression,
  real request-path (`run_conversation`) outage and recovery tests for the streaming,
  non-streaming and `direct_api_call` wrappers, and notice-retention tests. It also covers a
  cross-process regression (a cached fallback agent must honor a longer window that another
  process re-armed), stale-outage grace and pruning, the stranded fallback index, and exact
  clearing that reaches a cached agent at its next turn. Overload cases cover 529 with
  Retry-After and 503 without it through `run_conversation`, in-flight versus lapsed
  overload backoff, non-arming 500 and timeout, and non-finite record pruning.
- `tests/hermes_cli/test_fallback_cmd.py`: the `cooldowns` list and clear surface, plus `clear`
  keeping its chain meaning, and `status` on a non-finite record.
- `tests/agent/_shared_cooldown_stub.py`: loopback OpenAI-compatible stub and child runner;
  `failure_status` selects 429, 529, 503 (overload body) or 500.
- `evals/provider_fallback/probe_shared_primary_cooldown.py`: isolated multi-process E2E; every turn
  runs through `run_conversation`. A pass prints three `PROBE_OK:` lines; the third is the 529
  overload phase.

## Upstream status

No upstream equivalent was found during S0. This is fork-only until an equivalent
released implementation exists upstream.

## Retirement and rollback

Retire when the selected upstream release provides shared primary cooldown state,
fresh-agent adoption, atomic outage notices, recovery ownership, and the CLI
inspection controls. Roll back by reverting the commits carrying
`Fork-Patch: shared-primary-cooldown`.
