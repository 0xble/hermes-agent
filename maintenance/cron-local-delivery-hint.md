# Cron local delivery hint

Load when changing the cron run preamble (`cron/scheduler_prompt.py::_CRON_HINT`) or
how `deliver` is resolved.

Fork patch identity: `cron-local-delivery-hint`.

## Required behavior

Every cron run is prefixed with the cron hint. Its DELIVERY sentence must match the
job's real delivery lane:

- When every comma-separated `deliver` token is `local`, the run is told its final
  response is saved to local output only and is not sent to anyone. If the job's own
  instructions require a relay or send, the run does that itself with the named tool.
  A missing or empty `deliver` resolves to `local`, matching
  `cron/scheduler_delivery.py::_delivery_lane_value` and `_normalize_deliver_value`.
- Any other value, including mixed `local,origin`, keeps the upstream hint byte-for-byte.
- SILENT, FAILURE and RECURSION text is shared byte-for-byte. `_CRON_HINT_LOCAL` is
  derived from `_CRON_HINT` by replacing only the DELIVERY sentence, and import fails
  if that sentence drifts.

## Upstream

Upstream `main` still prepends the unconditional "automatically delivered" hint.
Closed, unmerged NousResearch/hermes-agent#44042 proposed a local-only variant against
the pre-split `cron/scheduler.py`. It also discouraged `send_message` unless the prompt
asked to notify a separate destination. Open #127174 and #127185 let jobs skip the hint
instead. Open issue #69495 asks for a customizable preamble.

## Verification and retirement

Run `scripts/run_tests.sh tests/cron/test_cron_local_delivery_hint.py tests/cron/test_scheduler.py tests/agent/test_synthetic_prompt.py`.
The focused regression fails on the pre-patch base for local, missing, empty, list,
repeated-local and padded-local `deliver` values. The system-level `PLATFORM_HINTS["cron"]`
sentence in `agent/prompt_builder.py` defers to the run's DELIVERY note, so both surfaces agree.
Failure notices still follow `failure_deliver`; the local sentence covers successful output.

Retire when a released upstream cron hint distinguishes local-only delivery and allows
instruction-required self-relay. Rollback: revert the commit carrying
`Fork-Patch: cron-local-delivery-hint`. No state or config migration is involved.
