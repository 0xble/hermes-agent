# Release readiness corrections from #910

Patch identity: `release-corrections-910`.

## Behavior

Memory-provider readiness at gateway boot is advisory and must never block startup:
- core memory-provider sentinels are skipped before plugin probes;
- handoff watch scopes resolve asynchronously, with the primary scope bound to the default root;
- a readiness reason survives malformed optional client package metadata;
- boot memory health checks are scheduled independently of startup.

Source surfaces: `gateway/runtime_health.py`, `hermes_cli/immutable_release_preflight.py`.
Landed from https://github.com/0xble/hermes-agent/pull/1248.

## Retirement

Retire when upstream's boot health and memory readiness checks have the same advisory,
non-blocking semantics. Roll back by reverting this logical patch.

Contract tests: `tests/gateway/test_memory_health.py`, `tests/gateway/test_memory_health_corrections.py`.
