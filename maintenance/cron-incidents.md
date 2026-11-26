# Cron Incident Classification

**Patch identity:** `cron-incident-classification`.

## Preserve

`cron/incidents.py` owns incident classification. Specific local argument-rejection
signals (`invalid_key`, `invalid_title`, `invalid_brief`, `intent_required`,
`self_relay`, CLI usage, and validation errors) belong to the existing `config`
category, ahead of incidental authentication or delivery context in an error
report. Authentication requires a failure signal, such as HTTP 401/403, invalid
credentials, Slack authentication error codes, an expired token, or proxy
`auth_unavailable`. Generic `auth`, `key`, and `token` words are not sufficient.

Preserve the existing category names, signature deduplication, lifecycle, and
stored historical incident classifications. There is no data migration.

## Update

The upstream classifier at `780d5a3313d7e1f64c0a0f3ec6b644d50f156e90`
matches `authentication` and `auth` before configuration. This mistakes successful
authentication context for the cause of a local validation failure. Extend the
existing ordered pattern table rather than moving classification into a router.
Upstream PR [#101099](https://github.com/NousResearch/hermes-agent/pull/101099)
addresses embedded payloads in the separate failure-delivery summarizer, not
incident classification, and does not replace this patch.

## Verify

Run the incident classification, incident store, and incident resolution tests
under an isolated `HERMES_HOME`. Regressions cover persisted local validation
errors, real provider/platform auth errors, and incidental authentication text.
Run the exact-head portable gate before integration.

Retire when the selected upstream release preserves these classification
contracts without the local patch. Rollback reverts the commit carrying
`Fork-Patch: cron-incident-classification`, without rewriting stored incidents.
