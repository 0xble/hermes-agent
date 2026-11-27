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

## Authored lane fields

`keep_pinned_lane` and `output_local_only` are authored job-definition fields in
`cron/job_definition.py::JOB_DEFINITION_FIELDS`. The external lane converter in the scripts
repository reads them, and live jobs set both. Distribution imports preserve them on new jobs
and replace either boolean on updates without touching scheduler-owned state.
`hermes cron create/edit --keep-pinned-lane/--no-keep-pinned-lane` and the cron tool's
`keep_pinned_lane` argument go through the shared job API. Omitting the flag on edit keeps the
current value.

`cron.wrap_response: false` also drops the pre-agent provider fallback notice from the
delivered report, so raw output stays exact. Default wrapping still prepends the notice, and
fallback resolution and its scheduler log records are unchanged. Silence, empty output and
failure markers stay unmodified in either mode.

These behaviors moved here from the retired [reviewed cron delivery](reviewed-cron-delivery.md)
unit on 2026-11-27.

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

Contract tests: `tests/cron/test_cron_local_delivery_hint.py`, `tests/hermes_cli/test_cron_pinned_lane.py`, `tests/cron/test_cron_pre_agent_fallback_notice.py`.
