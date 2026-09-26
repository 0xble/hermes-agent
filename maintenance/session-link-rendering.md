# Session-link rendering

Load this unit when changing session-search results, platform forwarding, or Telegram outbound link handling.

## Required behavior

Desktop session-search results retain clickable internal session references. Other active platforms receive the same titles and transcript data without `link` fields, plus runtime guidance to cite titles as plain text. This shaping occurs only on returned results: the registered model tool schema and system prompt remain byte-stable across turns. Both the inline executor and registry handler forward the active platform. Telegram's existing rich-send/edit/draft target guard remains the independent delivery boundary for unsupported Markdown targets.

## Provenance and patches

Fork patch identity: `session-link-rendering`.

Adapted from archived HERMES-065 (`session-context.md`) and Brian's open upstream PR [#97535](https://github.com/NousResearch/hermes-agent/pull/97535). The archived patch also covered Telegram delivery; current fork `origin/main` already carries that guard, so only the missing session-search producer boundary is adopted. Current upstream `main` at `d0288be5b3330d2442e3907185b8e9d0958297bb` still lacks producer shaping; upstream PR #97514 covers Telegram link-target degradation independently. The upstream PR will be refreshed onto current upstream `main` before adoption.

## Verification

`scripts/run_tests.sh -j 6 tests/tools/test_session_search.py tests/tools/test_registry.py tests/agent/test_token_persistence_non_cli.py tests/gateway/test_telegram_unsupported_link_targets.py tests/gateway/test_telegram_format.py tests/gateway/test_telegram_rich_messages.py -q`: 259 passed. RED on fork base: non-Desktop call rejected `platform`, and inline execution exposed `link`. A broader `tests/tools/` run timed out locally; the focused and sibling suites passed. Preserve both Desktop and non-Desktop rendering.

## Retirement and rollback

Retire after a released upstream version supplies equivalent platform-aware session-search results and the Telegram delivery guard, with tests passing without this fork patch. Roll back by reverting the fork patch commit; no persisted data or schema migration.
