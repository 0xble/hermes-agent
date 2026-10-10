# Goal continuation silence provenance

## Fork patch identity

Fork patch identity: `goal-continuation-silence`.

This patch keeps event-less interrupt and `/steer` follow-up text human-authored
when it is requeued after a goal-continuation turn. A real pending event remains
the authority for `internal`, `metadata`, and reply provenance. When no event is
available, there is no machine-generated marker to distinguish a person’s text,
so the gateway defaults to human provenance rather than inheriting the previous
turn’s goal-continuation contract.

## Verification

The regression is covered by `tests/gateway/test_goal_continuation_silence.py`.
The implementation is in `gateway/run_turn.py` at the recursion-cap,
failed-delivery, and recursive follow-up handoff sites.
