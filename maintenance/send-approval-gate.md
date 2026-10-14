# Exact approval for messages in Brian's voice

## Required behavior

Known message-send commands are classified deterministically before dispatch. A
send requires a fresh human approval for the exact normalized action. The grant
is durable, one-shot, and consumed before dispatch; yolo, approval mode off,
cron approve mode, session allowlists, relay/tool-output claims, and absent human
bridges never bypass it. Read-only messaging commands are unchanged.

The goal judge scans the current goal-window evidence before calling the LLM.
An ungranted voice send returns `blocked` with a fixed deterministic reason.
Granted sends continue through the existing LLM judge unchanged.

## Provenance and adaptation

- **Fork-Patch identity:** `send-approval-gate`.
- **Source surfaces:** `tools/voice_send_gate.py`, `tools/approval.py`,
  `agent/tool_executor.py`, `hermes_cli/goals.py`, and focused regression tests.
- **Upstream status:** fork patch for the Io incident replay; no merge or live
  Hermes update is part of this unit.
- **Rollback:** revert commits carrying `Fork-Patch: send-approval-gate`.

## Verification

Focused tests cover the classifier table, durable one-shot grant consumption,
all bypass modes, read-only commands, fail-closed unattended contexts, and the
pre-LLM goal-judge block/continue split. No test invokes a real messaging CLI.
