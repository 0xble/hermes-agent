# Proactive tool-result prune default

Load this unit when changing the default trigger or behavior of the deterministic tool-result prune.

## Required behavior

The maintained fork enables the existing no-LLM `ContextCompressor.prune_tool_results_only` path by default with `compression.proactive_prune_tokens: 48000`. It deduplicates identical tool results and summarizes eligible older oversized tool results once the billed history exceeds the trigger, while protecting the recent `protect_last_n` message tail. The existing `proactive_prune_min_result_chars: 8000` and `proactive_prune_min_reclaim_tokens: 4096` gates remain unchanged. Setting the trigger to `0` remains the explicit opt-out.

## Provenance and patches

Fork patch identity: `proactive-tool-result-prune-default`.

Fork-only default change. The deterministic pruning implementation is already shared with upstream; no equivalent upstream nonzero default was found during the upstream design preflight. Keep the fork default documented separately from the upstream baseline and revisit when upstream enables a nonzero default with equivalent tail protection, reclaim gating, and regression coverage.

## Verification

`python -m pytest -q tests/agent/test_proactive_prune_config.py tests/agent/test_proactive_tool_result_pruning.py tests/agent/test_proactive_prune_rearm_threshold.py tests/agent/test_proactive_prune_loop_wiring.py`

## Retirement and rollback

Retire the fork default change when released upstream enables an equivalent nonzero default; remove the fork-only default/doc/test adaptation while retaining equivalent upstream-owned coverage. Roll back by reverting the commit that changes `compression.proactive_prune_tokens` from `0` to `48000` and its default-specific documentation and regression test; no persistent-data migration is required.
