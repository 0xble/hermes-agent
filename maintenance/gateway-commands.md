# Gateway commands while busy: quick-command aliases and deferred session commands

Load this unit when changing the adapter active-session guard, the runner busy fast-path,
`quick_commands`, or which slash commands run or defer during an active turn.

## Required behavior

- Alias `quick_commands` (for example `s` → `/steer`) expand before both the adapter and
  runner busy-session guards, per routed-profile snapshot, so secondaries never inherit the
  primary's aliases. Aliases that target `/stop`, `/new`, or `/reset` keep ordinary busy
  semantics. Name-less alias targets are rejected before the busy-path guard.
- `/fast`, `/reasoning`, `/title`, `/usage`, and `/whoami` run during an active turn.
  `/compress`, `/undo`, `/retry`, `/save`, and `/branch` are acknowledged, keep their
  command identity, and execute ahead of queued follow-up text once the turn commits.

## Provenance and patches

- Fork patch identity: `quick-alias-busy`. Port of archived fork `52d54885f6dd`. Nearest
  upstream tracking is [issue 25783](https://github.com/NousResearch/hermes-agent/issues/25783)
  (exec type only; its PR 25804 closed unmerged).
- Defer-until-idle landed before the trailer floor as fork PR #7 (`ffffb081271f`),
  patch-equivalent to own contribution [upstream PR 116295](https://github.com/NousResearch/hermes-agent/pull/116295)
  at `9c133fd14aa5`, open on 2026-09-19.

- Follow-up patch identity: `busy-command-dispatch`. Moves the five immediate
  handlers into the shared idle/busy dispatch table. Registry policy alone did not
  make them reachable. Regression exercises `_handle_message` with an active agent
  and verifies handler invocation without interruption or queueing.

## Verification

`scripts/run_tests.sh` on `tests/gateway/test_command_bypass_active_session.py`,
`tests/gateway/test_session_race_guard.py`, `tests/gateway/test_busy_command.py`, and
`tests/gateway/test_running_agent_session_toggles.py`.

## Retirement and rollback

Retire alias expansion when upstream expands alias quick commands on the busy path. Retire
defer-until-idle when PR 116295 or equivalent is in the candidate release. Roll back by
reverting the logical patch; no persistent data changes.
