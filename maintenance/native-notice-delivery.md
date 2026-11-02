# Native Notice Delivery

Fork patch identity: `notice-delivery`.

## Contract

The existing keyed host-notice queue delivers once on the next model continuation
following a tool batch, or on the next user turn when no tool continuation runs.
Delivery must not append prose or metadata to tool-message content. A complete
JSON receipt must still parse and interrupted side-effecting receipts must retain
UNKNOWN replay disposition.

The native continuation boundary appends 1 new, self-identifying user-role host
notice after the complete batch. It combines a simultaneous human steer in the
same fresh row, retaining the steer's human authority without attributing host
text to the person. This is the narrow host-notice exception to the upstream
synthetic-user restriction, using the same supported assistant/tool/user sequence
as busy steer. It changes no plugin timing, origin policy or Autopilot behavior.
No historical message, system prompt or tool schema is rewritten. A redirect
consumes accepted guidance while creating its fresh correction row, before the
first persistence. Human guidance stays ordered before the newer correction.
The same host-frame composer handles both ordinary tool continuations and
redirects, including repeated redirects after an existing continuation row.
Deferred steer delivery returns transfer tickets through the native queue owner.
Generated post-tool snapshots and producer-marked standalone task restatements
also permit this continuation. A restatement still carries the original human
instruction, but is not new inbound input. Ordinary human tails, pasted replay
headers, and summary carriers with new human input continue to defer delivery.
The separator and control row are appended without changing the scaffold.

Producer-owned `display_metadata.runtime_notice` stays off the provider wire but
survives replay and compaction. It prevents timestamp injection from changing
notice bytes, and lets compression recognize the still-unread tool round. The
SessionDB structured-content codec preserves tool parts instead of projecting
them to screenshot markers. User/assistant projection is unchanged. This can
increase stored tool-image bytes, within the existing result/image limits.

## Verification And Ownership

`tests/agent/test_runtime_notice_receipts.py` drives real conversations, native
request construction and SDK serialization using a local HTTP transport. It
checks strings and multimodal parts, real SessionDB receipts, frozen history,
parallel ordering, concurrent steer, late notices, and UNKNOWN replay safety.
`test_plugin_live_notes_delivery.py` covers keyed replacement, no-tool fallback
and turn-budget spill. `tests/gateway/test_message_timestamps.py` checks 2 replay
cycles with compaction and human/host provenance. Existing affected steer,
compression, persistence and provider suites remain required.

The [patch register](../references/maintenance/operations/patch.md#notice-delivery)
owns pinned source/research evidence, exact check status and gated landing.
Parent owns review, integration and Hermes Maintenance handoff. This patch is
not activation authority. Live promotion remains held for the 4 AM PT window.

Revert the delivery change and its tests/docs to roll back. There is no schema,
config, dependency, service or store migration. Retire when upstream releases
provide the same full-receipt, once-only, replay-safe continuation contract.
