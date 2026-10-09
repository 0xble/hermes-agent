# Shared primary cooldown

## Fork patch identity

This maintenance unit owns the fork patch identity `shared-primary-cooldown`.

## Required behavior

A rate-limited, billing-limited, or upstream-rate-limited primary route records its
wall-clock cooldown under `$HERMES_HOME/state/model_cooldowns.json`. New agents,
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
  clearing that reaches a cached agent at its next turn.
- `tests/hermes_cli/test_fallback_cmd.py`: the `cooldowns` list and clear surface, plus `clear`
  keeping its chain meaning.
- `tests/agent/_shared_cooldown_stub.py`: loopback OpenAI-compatible stub and child runner.
- `evals/provider_fallback/probe_shared_primary_cooldown.py`: isolated multi-process E2E; every turn
  runs through `run_conversation`. A pass prints two `PROBE_OK:` lines.

## Upstream status

No upstream equivalent was found during S0. This is fork-only until an equivalent
released implementation exists upstream.

## Retirement and rollback

Retire when the selected upstream release provides shared primary cooldown state,
fresh-agent adoption, atomic outage notices, recovery ownership, and the CLI
inspection controls. Roll back by reverting the commits carrying
`Fork-Patch: shared-primary-cooldown`.
