# File-tool recovery diagnostics

Fork patch identity: `file-tool-recovery`.

## Decision and provenance

Keep bounded, transparent recovery inside the existing file/search/fuzzy owners,
not a plugin that wraps dispatch or guesses alternative edit targets. This patch
adds diagnostics to existing failures and a single literal-search retry; it does
not weaken edit uniqueness, approximate replace-all refusal, protected-path checks,
or environment-error classification.

Upstream guidance was inspected at `e06df9c2eae6901f4c88f464e0ae6e753c0428b8`.
[Upstream issue #66129](https://github.com/NousResearch/hermes-agent/issues/66129)
remains open and describes malformed-regex retry loops. The fixed-string retry
covers that direction without interpreting a content query as a filename glob.
[Upstream PR #82800](https://github.com/NousResearch/hermes-agent/pull/82800)
is merged: retain its unicode-equivalent filename repair and near-miss ranking;
this patch only stabilizes the missing-path diagnostics around that owner.
The Telegram variation-selector change belonged to the native topic tool, retired with
the [Telegram stack](telegram-stack-retirement.md), not this unit.

Delivery route: owned-fork PR only; no upstream publication or runtime promotion
is authorized by this change. The scoped source and dispatch proof lives in
`tests/tools/test_file_recovery_diagnostics.py`, with the existing error-guard,
native-search, fuzzy-match, and unicode filename tests as compatibility checks.

## Adoption and retirement

During upstream sync, compare the literal retry bound and warning on every output
mode (the retry note is appended to multiline and macOS protected-folder notes,
never replacing them), execution-backend path resolution (reported in
`resolved_path` while the probe and error keep the raw path), and ambiguity
locations (exact `match_count`, first 20 `match_lines`, `match_lines_truncated`
when more exist). Adopt
an equivalent upstream implementation only after the dispatch tests pass without
the fork code. Never turn approximate replace-all refusal into an automatic edit.
Retire this unit when those recovery contracts are upstream-equivalent. Rollback
reverts the logical patch; there is no persisted state to migrate.
