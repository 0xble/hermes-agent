# Copilot ACP usage-less compaction

Load this unit when changing `agent/copilot_acp_client.py` response construction, usage accounting, or estimate-driven preflight compaction for providers that report no token usage.

## Required behavior

ACP reports no token usage. The Copilot ACP client must return `usage=None` rather than a zero-valued usage object, so Hermes treats pressure as unknown and falls back to its rough message and tool-schema estimate. A fabricated zero reading looks like a real measurement and suppresses mid-turn auto-compaction, so long ACP sessions never compact.

The `[doctor-fix]` seeder in `tests/hermes_cli/test_config_edit_seed.py` asserts the `0600` config mode only for ordinary hosts. It isolates `hermes_constants._detect_container`, because containerised volume mounts intentionally skip chmod. The container branch is product behavior, not a test failure.

## Provenance and patches

Fork patch identity: `nightly-regression-0926`.

Found by the scheduled fork nightly on 2026-09-26 (run 36234305522 at `c8e334259`). `test_compaction_in_an_acp_session_keeps_the_next_prompt_valid_and_grounded` reproduced in a Linux container with zero auxiliary summarizer prompts. With `usage=None`, the summarizer received three. The E2E threshold was recalibrated from 21,000 to 15,000 estimated tokens to match measured pressure (about 14,058 after the eight reads). The text tool bridge is not counted as OpenAI tool-schema tokens. Upstream carries the same zero-usage construction and the same test (`e6db58be64c4`). An upstream contribution remains to be filed.

## Verification

`scripts/run_tests.sh tests/agent/test_copilot_acp_client.py tests/hermes_cli/test_config_edit_seed.py` on any host, plus `tests/e2e/core/providers/test_native_copilot_acp.py` on Linux (it skips elsewhere) as a non-root user.

## Retirement and rollback

Retire when a released upstream baseline stops fabricating ACP usage and its compaction E2E passes on the fork nightly. Roll back by reverting the patch commit. No persisted state changes.
