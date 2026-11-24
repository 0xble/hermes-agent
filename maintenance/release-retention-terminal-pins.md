# Release retention: finished cron executions do not pin releases

Fork patch identity: `release-retention-terminal-pins`.

## Required behavior

`hermes_cli.immutable_releases.retain()` keeps the current and previous releases,
recent rollback candidates, releases that a live process imports, receipt pins, and
releases named by the cron execution ledger (`~/.hermes/cron/executions.db`).

A ledger row pins its `code_sha` release only while it can still run: a NULL or
unrecognized status, `claimed`, `running`, or `unknown` (an owner that vanished
mid-run). Finished rows (`completed`, `failed`, `cancelled`, `missed`, see
`cron/executions.py` `_TERMINAL_STATES`) do not pin. Otherwise every release that
ever ran a cron job is kept forever. On 2026-11-23 the ledger's 84k finished rows
pinned all 32 releases (48.9 GB, 2.59M files), so `retain()` removed none. That
filled the disk and timed out the Hermes backup size walk.

The check stays fail closed. A ledger without a `status` column pins every row, and
an unreadable ledger raises instead of allowing deletion.

Contract tests: `tests/cron/test_immutable_worker_real.py`,
  `tests/hermes_cli/test_immutable_releases.py`.

## Upstream

Fork-only. Upstream has no immutable release retention.
