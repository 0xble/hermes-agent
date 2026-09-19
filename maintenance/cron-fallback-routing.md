# Cron fallback routing

Owns the `cron.fallback_providers` divergence. The [root contract](../MAINTENANCE.md)
owns release-baseline selection. Read this unit before adopting upstream changes
to cron preflight, provider recovery, agent construction, or fallback normalization.

## Provenance and adoption

- Upstream contribution: [NousResearch/hermes-agent#116220](https://github.com/NousResearch/hermes-agent/pull/116220),
  revision `79ed1f79550d4db77362af5abb0d97fcb6265072`, submitted and open at adoption.
- Fork patch identity: `cron-fallback-routing` (`Fork-Patch:` trailer).
- Required behavior: a cron override cannot inherit global entries, including
  legacy `fallback_model`. Omitted/null inherits, explicit empty disables.
- Fork adaptation: retain the release baseline's failure-notice wording while
  pointing to the cron setting. Do not import the newer upstream-only notice tests
  or its notice renderer. Routing implementation and new regression tests are unchanged.
- Source adoption does not activate the personal or company installations or
  change their model policy. Use the separately authorized runtime owner for that.

## Proof and retirement

The proof surface is `tests/cron/test_cron_fallback_config.py`, the existing
cron scheduler/preflight/failure-notice tests, and
`tests/hermes_cli/test_fallback_config.py`. Exercise configuration through a temp
profile and prove both credential recovery and agent construction use the same
chain without mutating the global policy.

Keep shared corrections synchronized with the upstream contribution first.
Retire this patch when the selected upstream release has equivalent semantics
and the routing regressions pass there. A PR merge alone does not establish
release-baseline coverage. To roll back source, revert this logical patch;
profiles using an explicit cron override must be reconciled before activation
because older code would resume global inheritance.
