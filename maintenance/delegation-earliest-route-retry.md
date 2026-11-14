# Delegation earliest-route retry

- **Patch identity:** `delegation-earliest-route-retry`
- **Contract tests:** `tests/tools/test_delegation_earliest_route.py`
- **Behavior:** Durable delegation retries use the configured route chain, but the shared store records only primary-route outages. A recorded active primary reset is combined with the provider reset hint and normal backoff floor; missing, malformed, or unreadable route state remains unknown. The retry cap remains six attempts within 24 hours.
- **Surfaces:** `agent/shared_primary_cooldown.py`, `tools/delegation_resume.py`, `tools/async_delegation.py`, `tools/delegate_tool_dispatch.py`, `agent/turn_failure_copy.py`, and `agent/turn_recovery.py`.
- **Retirement:** Remove this patch and its focused tests when upstream provides equivalent earliest-route retry scheduling and lineage-scoped availability notice suppression.
- **Rollback:** Revert the commit carrying `Fork-Patch: delegation-earliest-route-retry`.
