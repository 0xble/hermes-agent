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
- Follow-up fork patch identity: `fallback-reasoning-effort` (`Fork-Patch:` trailer).
  A selected fallback entry's valid `reasoning_effort` overrides the active route;
  invalid values warn and use normal per-model/global resolution. Primary reasoning is
  snapshotted and restored. The proof surface is the real `try_activate_fallback`
  path in `tests/agent/test_provider_fallback.py` plus the primary-runtime and cron
  reasoning suites. Retire when upstream PR [#45961](https://github.com/NousResearch/hermes-agent/pull/45961)
  or an equivalent released change covers these semantics; roll back by reverting
  this logical patch.
- Source adoption does not activate the personal or company installations or
  change their model policy. Use the separately authorized runtime owner for that.

## Script Sender Boundary

Fork PR [#657](https://github.com/0xble/hermes-agent/pull/657) also uses this
patch identity for cron's truthful relay sender. Scripts with a known job ID
receive `HERMES_SESSION_ID=cron_<job_id>`, the owning profile, and
`HERMES_CRON_SESSION=1` after the session sanitizer. No-agent scripts run before
`_CronRunScope` binds ContextVars, so their explicit job ID establishes cron
execution. Unknown-job scripts remain unmarked. Ambient sender, routing and
auto-delivery values must not be restored.

`tests/cron/test_script_context_propagation.py` exercises real `run_job` monitor,
pre-run and no-agent subprocesses, including the claimed one-shot heartbeat
wrapper. `tests/cron/test_cron_script.py` covers the known/unknown-job boundary.
Retire this extension when upstream has equivalent sanitized script identity.

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
