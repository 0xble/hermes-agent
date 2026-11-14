# Delegation earliest-route retry

- **Patch identity:** `delegation-earliest-route-retry`
- **Contract tests:** `tests/tools/test_delegation_earliest_route.py`
- **Behavior:** Durable delegation retries inspect every configured primary and fallback route in the shared cooldown store and schedule the earliest known recovery. Missing, malformed, or unreadable state falls back to the existing backoff and provider reset hint; the retry cap remains six attempts within 24 hours.
- **Surfaces:** `agent/shared_primary_cooldown.py`, `tools/delegation_resume.py`, `tools/async_delegation.py`, `tools/delegate_tool_dispatch.py`, `agent/turn_failure_copy.py`, and `agent/turn_recovery.py`.
- **Retirement:** Remove this patch and its focused tests when upstream provides equivalent earliest-route retry scheduling and lineage-scoped availability notice suppression.
- **Rollback:** Revert the commit carrying `Fork-Patch: delegation-earliest-route-retry`.
