# Steer Routing

Identities: `steer-delivery-observer`, `busy-input-auto`

## Divergence

Ask Tracking's out-of-tree plugin needs a delivery witness for mid-turn input.
Normal turn hooks contain the admitted message, while native inserted steer rows
have no stable delivery identity. Transcript capture is not a substitute.
Consumer: https://github.com/0xble/agents/pull/1043.

The fork adds `post_steer_delivered`. Its public payload and batching behavior
are owned by the developer plugin guide. `agent/steer_delivery.py` owns the
observer and requeue helper. Auto steering extends that existing event with
host-separated manual text and original-message descriptors, rather than
requiring consumers to parse wrappers.

`busy-input-auto` is a related native-routing divergence. Ordinary plain text
can stay in the active turn or return to its host's queue through
`defer_message`. Hooks cannot atomically hand back the original queue envelope,
so the shared receipt primitive belongs in core. The gateway, CLI, and TUI reuse
it rather than maintaining independent model-visible IDs. The tool schema stays
stable, with availability checked against the active turn at dispatch.

Related upstream PR [#31500](https://github.com/NousResearch/hermes-agent/pull/31500)
was closed without merge. Its `auto_steer` proposal describes dropping instead
of queueing in the priority fallback, which does not meet this fork's contract.
The upstream guidance inspected for this design was pinned at
`cb757fac59c92b49e9b8f2910e5cd7ab75efffdd`. No public contribution is authorized
by this record.

## Preserve

Observer callbacks remain nonfatal and bounded by the existing hook timeout.
An insertion receipt is not provider-acceptance or process-death persistence
proof. Missing turn/platform attributes keep their empty-string fallbacks.

Auto receipts retain the host's original envelope, arrival order, and reply
anchor. A deferred envelope cannot merge with later text or photos. Repeated
handoffs are idempotent. Input accepted after the last model request returns to
the original host queue at finalization. Slash commands, media, explicit
`/queue`, internal events, interrupt, and `/stop` retain their existing paths.

Arrival order is assigned on input admission, including explicit queue and media
admissions, never at a delayed handoff. The gateway retains its event stamp.
The TUI keeps an arrival sequence on the existing queue envelopes and merges
only adjacent queued text, not across an intervening auto input. The CLI extends
its native `Queue`, retaining arrival metadata internally while consumers still
receive the original string, image tuple, or voice sentinel. Queue edits retain
that metadata. A successful insertion commits the handoff, so a later display
failure cannot reopen its receipt or enqueue the original input again.

The gateway marks a deferred envelope with an event attribute, never
`metadata`: durable native admission has a closed metadata contract and refuses
any other key, so a metadata marker would turn every drained deferral into an
`invalid_params` refusal. The deferral returns the original to the adapter
pending slot that main's own steer and interrupt fallbacks use. Under a session
authority it reaches the durable FIFO when that slot drains, so its position
relative to rows the authority admitted directly, such as `/queue`, follows
that drain rather than the original arrival stamp. Desktop has no deferral path
and treats backend `auto` as `queue`.

The Ask Tracking consumer update must be qualified with the compatible core
before enabling auto mode. Optional `manual_text` and `auto_messages` are
host-owned input boundaries. The consumer observes successful deferrals through
the existing `post_tool_call` hook and never infers completion from a handoff.

## Adoption

On upstream adoption, inspect both native insertion sites, per-turn binding,
late-input finalization, and each host queue. Retire the observer divergence only
when upstream exposes an equivalent insertion-time event that its consumer can
use without transcript inference. Upstream PR 106072 was closed for an
adapter-owned acceptance hook, not actual row delivery.

Retire auto routing only when a released upstream implementation preserves the
complete absorb-or-defer contract across these surfaces. Configure another
supported busy mode before rolling back to code that does not accept `auto`.
Revert only the auto-routing commit and companion consumer change. Keep the
pre-existing observer, manual steer behavior, and unrelated fork patches.

## Proof

Use the canonical `scripts/run_tests.sh` runner for
`tests/agent/test_steer_deferral.py`,
`tests/agent/test_steer_delivered_hook.py`,
`tests/agent/test_compression_last_assistant_anchor.py`,
`tests/gateway/test_busy_auto.py`,
`tests/tui_gateway/test_auto_steer.py`, and
`tests/hermes_cli/test_auto_steer.py`, plus the existing steer, busy-policy,
queue, lock-fallback, and plugin suites for the affected surfaces.

The consumer's native probe checks real plugin dispatch and durable capture with
disposable state. Live Telegram must additionally prove absorption of a related
message and deferral of unrelated input into a separately anchored next turn.
Source tests do not establish live activation.
