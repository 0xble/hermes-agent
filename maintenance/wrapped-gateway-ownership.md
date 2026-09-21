# Wrapped gateway service ownership

## Contract and placement

Service-manager PIDs can identify a wrapper rather than the actual gateway.
Expand their exclusion set with recursively discovered descendants matching the
canonical gateway command parser. Preserve service/profile scope, keep genuine
manual gateways eligible, and tolerate exited or inaccessible processes.
This is a native updater and reaper safety invariant. No plugin or configuration
hook owns process exclusion, so a core patch is the smallest adequate repair.

Fork patch identity: `wrapped-gateway-service-ownership`.

## Evidence and provenance

On 2026-09-21, the installed launchd service reported wrapper PID 96522 while its
real gateway child was 96554. The existing finder excluded only the wrapper and
returned the child as a manual gateway. The preceding update recorded an unmapped
PID kill after restarting launchd, then timed out with an empty fleet receipt.
The replacement subsequently reported the correct code through its control
socket. The historical partial receipt remains truthful and must not be rewritten.

Adopt the source change from [upstream PR #106053](https://github.com/NousResearch/hermes-agent/pull/106053),
head `760c38d413de1acb9216b704bc98b57d4e9c1fa9`, by gaoanze888, preserving
Tranquil-Flow attribution. That PR was closed in favor of the original
[PR #66913](https://github.com/NousResearch/hermes-agent/pull/66913), still open at
head `2f83b8c535a4ff9efcab610a46f5fe078e6b7482`. It is a temporary unreleased
backport, not upstream acceptance. The independent diagnosis called for service
ancestry exclusions, and the proposed recursive strict-parser implementation
matches that contract without new heuristics or supervisor machinery. Upstream root
and CLI guidance was checked at `996417c385a5b0dcb15db1d1e0867d9869c7680d`: preserve
canonical argv matching, fleet restart ownership, snapshot locking, and truthful
post-update receipts. The source adaptation is identical to PR #106053. Tests
isolate live-host service discovery and add a real-process manual-sweep boundary.

## Verification and retirement

Run scripts/run_tests.sh for tests/hermes_cli/test_gateway_launchd_supervised_child.py,
tests/hermes_cli/test_gateway_proc_fallback.py,
tests/hermes_cli/test_update_launchd_unloaded_gateway.py, and
tests/hermes_cli/test_gateway.py. The new boundary test starts an isolated real
wrapper and gateway-shaped child, reads actual process ancestry/argv, and proves
the actual updater manual sweep with signal recording: the wrapped child is
protected while a manual candidate remains eligible. Service lookup is simulated.
No real gateway or messaging transport is started or signalled by these tests.

Retire when the selected upstream release passes these regressions without the
local patch. Roll back by reverting the change. Activation must use native update
and verify the receipt, live identity, and separate functional behavior. Backup
locking and the bounded fleet verification window remain unchanged.
