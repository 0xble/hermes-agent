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

`hermes fallback status` displays active records and `hermes fallback clear [model]`
clears records without changing the configured fallback chain when a model is given.

## Source surfaces and proof

- `agent/shared_primary_cooldown.py`: file-locked, atomic per-`HERMES_HOME` state.
- `agent/fallback_cooldown.py`: writer and shared backoff escalation.
- `agent/agent_runtime_helpers.py`: fresh-agent adoption and turn-start gating.
- `agent/chat_completion_helpers.py` and `agent/chat_completion_nonstream.py`: notice claim and recovery clear.
- `hermes_cli/fallback_cmd.py`, `hermes_cli/subcommands/fallback.py`: status/clear controls.
- `tests/agent/test_shared_primary_cooldown.py`: separate-process persistence, one-claim regression,
  real request-path (`run_conversation`) outage and recovery tests for the streaming,
  non-streaming and `direct_api_call` wrappers, and notice-retention tests.
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
