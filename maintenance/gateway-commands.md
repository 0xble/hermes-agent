# Gateway commands while busy: quick-command aliases and deferred session commands

Load this unit when changing the adapter active-session guard, the runner busy fast-path,
`quick_commands`, or which slash commands run or defer during an active turn.

## Required behavior

- Alias `quick_commands` (for example `s` → `/steer`) expand before both the adapter and
  runner busy-session guards, per routed-profile snapshot, so secondaries never inherit the
  primary's aliases. Aliases that target `/stop`, `/new`, or `/reset` keep ordinary busy
  semantics. Name-less alias targets are rejected before the busy-path guard.
- `/fast`, `/reasoning`, `/title`, `/usage`, and `/whoami` run during an active turn.
  `/compress`, `/undo`, `/retry`, `/save`, `/branch`, and `/moa <prompt>` are acknowledged,
  keep their command identity, and execute ahead of queued follow-up text once the turn commits.
- `/moa <prompt>` mid-run never switches the running agent. Gateway: deferred, then replayed
  through idle `_hm_cmd_moa`; the turn finalizer restores the prior override, including a
  standing `/model` override. `/stop`, `/new`, `/reset` drop it. Bare `/moa` returns usage.
  Ink TUI: `_cmd_moa` records `pending_moa` and returns `send` with `queued: true`; Ink always
  enqueues that prompt (never steer/interrupt), and the matching queued turn applies and restores it.
- Ordering caveat (`busy_input_mode: queue`): plain text queued while busy drains inside the running
  turn, before deferred commands, so text sent after `/moa` runs before the MoA turn, on the prior model.

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

- Follow-up patch identity: `moa-busy-defer`. TUI half contributed as
  [upstream PR 132644](https://github.com/NousResearch/hermes-agent/pull/132644); the gateway half
  waits on upstream `defer_until_idle` (PR 116295 / 125345).

## Verification

`scripts/run_tests.sh` on `tests/gateway/test_command_bypass_active_session.py`,
`tests/gateway/test_session_race_guard.py`, `tests/gateway/test_busy_command.py`, and
`tests/gateway/test_running_agent_session_toggles.py`; for `moa-busy-defer` also
`tests/gateway/test_moa_busy_defer.py`, `tests/tui_gateway/test_moa_busy_defer.py`,
`tests/hermes_cli/test_tui_rapid_enter_paste.py`, and `ui-tui` `createSlashHandler.test.ts`.

## Retirement and rollback

Retire `moa-busy-defer` when PR 132644 (or equivalent) and an upstream `defer_until_idle` that
lists `/moa` are both in the candidate release.
Retire alias expansion when upstream expands alias quick commands on the busy path. Retire
defer-until-idle when PR 116295 or equivalent is in the candidate release. Roll back by
reverting the logical patch; no persistent data changes.
