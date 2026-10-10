# Goal continuation silence provenance

## Fork patch identity

Fork patch identity: `goal-continuation-silence`.

This patch defaults event-less interrupt and `/steer` follow-up text to human
provenance when it is requeued after a goal-continuation turn, except for
agent-origin relay text, which keeps its relay reply expectation. A real pending
event remains the authority for `internal`, `metadata`, and reply provenance.
When no event is available, a complete relay header at either edge of the text
permits silence; other text requires a reply rather than inheriting the previous
turn’s goal-continuation contract. Leftover `/steer` text includes the gateway
origin preamble, so the gateway removes only that complete envelope before
applying the existing relay-header predicate. Raw interrupt text is unchanged.

## Verification

The regression is covered by `tests/gateway/test_goal_continuation_silence.py`,
using the real steer-origin wrapper for both deferred relay and human follow-ups.
The implementation is in `gateway/run_turn.py` at the recursion-cap,
failed-delivery, and recursive follow-up handoff sites. `gateway/run_busy.py`
owns the shared origin-preamble constants and envelope parser.
