# Agent-Declared Cron Failure Metadata

**Patch identity:** `cron-agent-failure-metadata`.

## Preserve and update

The producer, not the downstream notification router, owns presentation unwrapping
and the declared incident type. Keep this boundary through upstream scheduler
refactors, including ordinary completion and canonical receipt recovery. Read
[Cron incident classification](cron-incidents.md) before changing type selection;
explicit script attribution still wins.

The persisted summary is the first meaningful non-fence line of the declared
failure evidence. This is a presentation rule, not a judgment of summary quality.
An agent-derived `script` classification falls back to `agent`; only the script
runner's `_script_failed` signal selects script attribution and its signature
namespace.

Upstream strict-marker work (#112431/#113155) supplies the declaration contract,
but persists leading Markdown fences as incident evidence. Related open
upstream delegation/blocked-tool proposals do not replace this metadata fix.
No upstream contribution is opened by this draft-only fork delivery.

Do not rewrite stored incidents. This patch changes new producer records only;
the exact original response remains in the saved output rather than being
reconstructed from the normalized summary.

Unwrapping a leading fence changes the stored error text used by
`_error_signature`. An agent-declared failure whose evidence previously began
with a fence therefore gets a new signature after the upgrade. If the old
incident was already acknowledged, the next matching failure can re-alert once
under the new signature. There is no migration of historical incidents or
acknowledgments.

## Proof and retirement

The behavior boundary is exercised in `tests/cron/test_agent_failure_metadata.py`
and `tests/cron/test_canonical_receipt_bookkeeping.py`. The audit fixture replaces
private URLs; private incident replay is not a source fixture dependency.

Retire when the selected upstream release preserves meaningful summary/type
metadata at both producer completion paths. Rollback reverts the commits bearing
`Fork-Patch: cron-agent-failure-metadata`, without editing historical records.

Contract tests: `tests/cron/test_agent_failure_metadata.py`, `tests/cron/test_canonical_receipt_bookkeeping.py`.
