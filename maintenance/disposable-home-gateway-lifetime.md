# Disposable one-shot gateway lifetime

Fork patch identity: `disposable-home-gateway-lifetime`.

## Contract

A finite CLI client (`-z`, `-Q`, stream-json and noninteractive `-q`) that
actually auto-starts an unmanaged owner holds a private lifetime pipe. Closing
the connection or exiting the client closes the pipe. Client SIGTERM/SIGKILL
also produces EOF. The gateway watches before heavyweight startup and requests
shutdown, with a three-second hard self-exit bound if shutdown wedges. Before
signaling, reuse only the gateway process's already-loaded foreground/background
tool registries to kill its own detached tool children (no host-wide PID scan).
The session-only startup retains the ordinary bounded MCP-discovery barrier so
the first finite turn does not lose eager MCP tools. Only the
creating client's Popen handles confer cleanup authority; discovered owners and
installed services are never stopped. Interactive sessions keep the existing
reusable idle-exit behavior.

The finite-client owner serves local sessions only. It must not dispatch cron,
restore heartbeat/loop/recovered work, connect messaging adapters, or start
long-lived daemon housekeeping. A disposable home is not a daemon enrollment.

## Design and upstream

Independent proposal: option (a), a private stdin pipe rather than PID polling
or a host-wide scheduler. A short idle timer still fails closed on persistent
work or a deleted home; a host cap adds coordination without establishing
ownership. The pipe also covers death while gateway imports are wedged.

Upstream guidance pinned at `e8639fd4312e5102d6f10c76b56406cb01c19092`:
root and subsystem AGENTS plus CONTRIBUTING. Searches covered open/closed PRs
and issues for idle-exit, one-shot gateway and gateway lifetime. Related upstream
PR NousResearch/hermes-agent#145864 handles a deleted store poisoning idle-exit
probes, not finite client lifetime. PR #143630 addresses inherited launchd markers,
not ownership. Neither eliminates the detached lifetime after successful client
exit. No equivalent caller-death fence found. Intentional fork divergence until
an equivalent upstream lifetime and daemon-service fence ships.

## Verification and retirement

Contract tests: `tests/hermes_cli/test_gateway_client_lifetime.py`
Contract tests: `tests/gateway/test_client_owned_bootstrap.py`

Run through `scripts/run_tests.sh` with checkout-owned test dependencies and temp
homes only. The subprocess regression drives the real connect/ensure/Popen path,
with a fake provider/gateway boundary, for concurrent normal exit, SIGTERM and
SIGKILL. It asserts process death and canonical runtime absence within seconds.
Existing runtime discovery/service, idle-exit and gateway startup tests preserve
interactive and supervised behavior.

Retire when released upstream bounds finite-client auto-starts including killed
clients and prevents those owners from running unrelated daemon services.
Rollback by reverting commits carrying the patch identity; no persistent config
or state migration is involved. No live runtime rollout is part of this patch.
