# Exact approval for messages in Brian's voice

## Required behavior

Known message-send commands are classified deterministically before dispatch. A
send requires a fresh human approval for the exact normalized action. The grant
is durable, one-shot, and consumed before dispatch; yolo, approval mode off,
cron approve mode, session allowlists, relay/tool-output claims, and absent human
bridges never bypass it. Read-only messaging commands are unchanged.

The voice-send floor lives in `model_tools._pre_dispatch_guards`, the shared
choke point used by direct registry dispatches, normal agent tool execution, and
`execute_code` nested calls. The agent executor does not run a second voice gate,
so one call gets one authorization decision. Nested calls inherit the current
session identity where available.

The classifier covers the supported Slack, iMessage, gog Gmail, wacli, Quo, and
Telegram CLI send verbs, including Slack message edits and Gmail `drafts send`.
It recursively inspects shell `-c` payloads and shell operators. Script content
is inspected only for an executed command word (including `env`/`nohup`/absolute
path forms) or the first script argument of a supported interpreter; `python -m`
module invocations do not scan their later test paths. The source-text regex
fallback is limited to executed non-shell scripts. This is a best-effort guard
against accidental sends, not an adversarial shell sandbox; it does not execute
or emulate scripts.

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
  `model_tools.py`, `tools/code_execution_rpc.py`, `hermes_cli/goals.py`, and
  focused regression tests.
- **Upstream status:** fork patch for the Io incident replay; no merge or live
  Hermes update is part of this unit.
- **Rollback:** revert commits carrying `Fork-Patch: send-approval-gate`.

- **Classifier precision:** send verbs are matched only at the CLI's command/subcommand position; embedded words in read-only arguments are ignored. Inline `python -c`, `node -e`, Ruby/Perl/Bun, and Deno eval sources use the same bounded fallback as executed scripts.
- **Grant accounting:** consumed grants are stored as timestamp lists and matched one-to-one to evidence rows within 120 seconds, so one grant cannot bless repeated identical sends.
- **Hook ordering:** plugin pre-tool hooks run before the voice gate; the gate hashes the final modified arguments and remains the single nested-dispatch choke point.
- **Judge refusal parsing:** the deterministic floor recognizes the registry's JSON `error` wrapper around gate refusals.

## Verification

Focused tests cover the classifier table and shell/script recursion, the shared
choke point including execute_code-style dispatch, durable one-shot grant
consumption, standing cron grants and their non-matches, all bypass modes,
read-only commands, fail-closed unattended contexts, and the pre-LLM goal-judge
block/continue split. No test invokes a real messaging CLI; CLI `--help` is the
only permitted command-level inspection.
