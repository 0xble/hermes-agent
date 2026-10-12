# Goal continuation silence provenance

## Fork patch identity

Fork patch identity: `goal-continuation-silence`.

This patch defaults event-less interrupt and `/steer` follow-up text to human
provenance when it is requeued after a goal-continuation turn, except for
agent-origin relay text, which keeps its relay reply expectation. A real pending
event remains the authority for `internal`, `metadata`, and reply provenance.
When no event is available, each constituent steer must independently prove
unaddressed relay provenance before the batch permits silence. Human, unknown,
addressed, or damaged-envelope constituents require a reply. Leftover `/steer`
text includes the gateway origin preamble, so the gateway parses each complete
envelope before applying the existing relay-edge predicate. The envelope carries
`reply_expected` unchanged, including when routing identifiers are privacy-hashed;
explicit `True` always requires a reply. Raw interrupt text is unchanged.

## Verification

The regression is covered by `tests/gateway/test_goal_continuation_silence.py`,
using the real steer-origin wrapper for both deferred relay and human follow-ups.
The implementation is in `gateway/run_turn.py` at the recursion-cap,
failed-delivery, and recursive follow-up handoff sites. `gateway/run_busy.py`
owns the shared origin-preamble constants and envelope parser.
