# Gateway delegation mode convergence

Load this unit when changing how a continuing gateway session switches between
joined and background `delegate_task` execution after an operator config change.

Fork patch identity: `delegation-mode-convergence`.

## Contract

`delegation.gateway_mode` is read from the current effective profile at a prompt-refresh
or compaction cache break. Only agents wired by the gateway turn runner may
converge. An explicit mode replaces only the session's pinned
`delegate_task` schema, re-freezes the agent's dispatch decision, and persists
the pair together, including the backend that built the refreshed schema.
Malformed or unavailable config leaves the existing mode unchanged. Ordinary
turns keep the existing `tools[]` pin, and one-shot sessions keep their own
joined behavior. Unrelated tool schemas and their pinned bytes are unchanged.

## Verification

Contract tests: `tests/tools/test_delegate_gateway_inline.py`,
  `tests/agent/test_session_prompt_refresh.py`.

## Rollback

Revert the commit carrying `Fork-Patch: delegation-mode-convergence`.
