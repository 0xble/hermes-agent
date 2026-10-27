# Auxiliary Retry And Timeout Bounds

Load when changing auxiliary same-provider retry counts, full-budget timeout policy, or compression timeout resolution.

## Provenance And Adoption

Fork patch identity: `auxiliary-retry-bounds`.

- Async retry counts adapt merged upstream [#56889](https://github.com/NousResearch/hermes-agent/pull/56889), commit `fb403a3a73`, to use the existing bounded configuration reader on both call paths. Closed, unmerged [#65642](https://github.com/NousResearch/hermes-agent/pull/65642) independently identified missing async backoff before its author closed it. Retain the existing sync backoff rather than introduce its jitter or superseded cache cleanup.
- Goal judging ports the 2 production-line changes from Yuan Li's open [#131515](https://github.com/NousResearch/hermes-agent/pull/131515), commit `5c60f77faebc9689349f94c70bca713ccae8b1f8`. Keep the 30-second default aligned with the goal reader. Entry-point coverage replaces the upstream private-helper test.
- Compression adapts Eric Curtin's open [#127083](https://github.com/NousResearch/hermes-agent/pull/127083), head `e41361efb9b4596249b7686809ebdddf2f6cbbc1`, for issue [#126769](https://github.com/NousResearch/hermes-agent/issues/126769). Explicitness is the presence of `auxiliary.<task>.timeout` in the session's authored config: the frozen worker snapshot when one is bound, otherwise `load_user_config_effective` (managed overlay, `${VAR}` expansion, cached on the file signature). A bypass session freezes a copy of `DEFAULT_CONFIG` and has no authored file, so a task block identical to the shipped default is not a pin. A frozen session auxiliary policy remains authoritative over live profile files.
- Goal judging keeps the 30-second default from Yuan Li's open [#131515](https://github.com/NousResearch/hermes-agent/pull/131515). Retained `~/.hermes/logs/agent.log*` INFO timings cover 2026-10-27 08:34–10:37 PT only (rotation does not keep 7 days). Pairing each `Auxiliary goal_judge: using` line with the next `goal judge: verdict` gave n=160, p50 4.71s, p95 8.50s, p99 11.36s, max 14.67s, none at or above 25s. The 7-day `gateway.error.log` has fallback stalls already past the 60s no-progress window (elapsed 60–287s), which exceed both 30s and 60s, so they do not argue for a longer success budget. Keep 30s.

The shared retry-count reader moves to the existing task-config owner and remains re-exported from the auxiliary facade. No new retry framework is introduced.

The open timeout proposals are not released upstream acceptance. This patch does not adopt a shared deadline guard or replace the fallback ladder. Keep the existing [compression route deadline](compression-route-deadline.md) contract separate.

## Verification

```bash
scripts/run_tests.sh tests/agent/test_auxiliary_retry_bounds.py tests/agent/test_auxiliary_compression_timeout_floor.py tests/agent/test_auxiliary_client.py tests/agent/test_compress_context_progress_timeout.py tests/hermes_cli/test_goal_judge_timeout_config.py
```

## Retirement And Rollback

Retire each adaptation when the selected released upstream baseline passes the corresponding entry-point regression without it. Revert the commit carrying this identity to roll back. There is no state migration, runtime configuration edit, or release promotion.
