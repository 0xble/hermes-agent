# Exact approval for messages in Brian's voice

## Required behavior

Known message-send commands are classified deterministically before dispatch. A
send requires a fresh human approval for the exact raw command or code text. The grant
is durable, one-shot, and consumed before dispatch; yolo, approval mode off,
cron approve mode, session allowlists, relay/tool-output claims, and absent human
bridges never bypass it. Read-only messaging commands are unchanged.

The voice-send floor lives in `tools/registry.py::ToolRegistry.dispatch`, the shared
execution choke point used by direct registry dispatches, normal agent tool execution, and
`execute_code` nested calls. Request middleware and pre-tool hooks in `model_tools._pre_dispatch_guards`
run before registry dispatch; the agent executor does not run a second voice gate, so one call gets
one authorization decision. Nested calls inherit the current session identity where available.

The classifier scans every standalone supported CLI occurrence in terminal
commands, interpreter sources, execute_code source, and executed script content.
An occurrence is read-only only when tokenization succeeds, the CLI is in a
recognized command-word position, and its full path is in the explicit
per-CLI read-only allowlist. Any unproven occurrence is gated, including
heredocs, shell control syntax, launchers, loops, xargs, quoted subprocess
literals, unknown verbs, and unknown flags. This is a fail-closed approval floor;
it does not execute or emulate scripts.

Unattended cron sends remain fail-closed: cron has no standing approvals and
there is no `voice_send` configuration key.

The goal judge scans the current goal-window evidence before calling the LLM.
An ungranted voice send returns `blocked` with a fixed deterministic reason. A
row whose output begins with the gate's own `BLOCKED: approval required before
sending a message in Brian` text is treated as a refused send, not a completed
send. GoalManager supplies the previous judged-turn timestamp to this floor, so
an earlier-turn send cannot block every later evaluation; direct `judge_goal`
callers retain whole-window checking when they use the default. Granted sends
continue through the existing LLM judge unchanged.

## Provenance and adaptation

- **Fork-Patch identity:** `send-approval-gate`.
- **Source surfaces:** `tools/voice_send_gate.py`, `tools/approval.py`,
  `tools/registry.py`, `model_tools.py`, `tools/code_execution_rpc.py`,
  `hermes_cli/goals.py`, and focused regression tests.
- **Upstream status:** fork patch for the Io incident replay; no merge or live
  Hermes update is part of this unit.
- **Rollback:** revert commits carrying `Fork-Patch: send-approval-gate`.

- **Classifier precision:** the raw-text occurrence scan is fail-closed; only proven read-only command positions are cleared. The exact raw command/code text is hashed.
- **Grant accounting:** consumed grants store tool-call IDs and timestamps. Evidence with a tool-call ID matches only that ID; legacy evidence without an ID retains the bounded timestamp fallback.
- **Hook ordering:** plugin pre-tool hooks run before the voice gate; the gate hashes the final modified arguments and remains the single nested-dispatch choke point.
- **Judge refusal parsing:** the deterministic floor recognizes the registry's JSON `error` wrapper around gate refusals.

## Verification

Focused tests cover the classifier table and shell/script recursion, the
structural fail-closed regressions, the shared choke point including an
execute_code subprocess send blocked before its handler runs, durable one-shot
grant consumption, tool-call-ID grant matching beyond the timestamp window, all
bypass modes, read-only commands, fail-closed unattended contexts, and the
pre-LLM goal-judge block/continue split. Cron has no standing approvals, so no
standing-grant test exists. No test invokes a real messaging CLI; CLI `--help`
is the only permitted command-level inspection.
