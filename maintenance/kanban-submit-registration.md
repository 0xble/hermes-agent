# Kanban submitter registration hardening

Fork patch identity: `kanban-submit-registration`.
Contract tests: `tests/e2e/core/kanban/test_kanban_dispatch_registration.py`.
Contract tests: `tests/hermes_cli/test_kanban_submitter_registration.py`.

## Required behavior

A dispatcher submitter keeps the base registration-before-admission contract. This
hardening makes registration fail closed when `HERMES_KANBAN_DB` is missing or
unreadable, before SQLite can create or open an unintended path. SQLite errors from
that registration block are reported as owner refusals. Owner startup and admission
errors retain their traceback.

## Diagnosis

Base `bf8d7a39891` already registers the submitter in
`hermes_cli/kanban_worker_client.py` before owner admission. This delta does not
move or duplicate that behavior. It only tightens the registration path with
strict resolution and limits SQLite error handling to that block. The 5-window
SIGKILL E2E is a guard for the base contract and is reported separately from this
hardening.

## Scope and retirement

Stack fit: existing Python worker client, SQLite transaction and PID adoption,
pytest, recording fake provider and real-process board harness. No dependencies,
schema, configurable hooks, timeouts, retries, or live runtime changes. The source
delta is limited to registration path resolution and exception scope.

Prior art checked: upstream PR 141187 concerns single-query registration-exit
accounting and fingerprint reads. It does not provide this registration-path
hardening. Retire this patch when the selected upstream release fails closed on
the same path and preserves traceback visibility for owner errors.
Rollback is a source revert. No database migration or runtime activation belongs
to this source-only draft candidate.
