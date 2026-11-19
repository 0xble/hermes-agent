# Proxy request class

Fork patch identity: `proxy-request-class`. This fork-only patch supplies the CLIProxyAPI scheduling contract; a static `extra_headers` entry cannot distinguish human and background calls. No upstream equivalent was identified at `3371c03df62106a4c93a545787a471e328883d08`. Upstream [#69233](https://github.com/NousResearch/hermes-agent/pull/69233) addresses named identity for static headers, not this dynamic classification.

Read [turn-origin provenance](turn-origin-provenance.md) when adapting ingress. Preserve named provider identity across auxiliary resolution and client reuse: two entries may share a URL and credential but have different opt-ins. The scheduling hint is not authorization and does not change local admission, health, or fallback policy. Keep the [provider setup documentation](../website/docs/user-guide/configuring-models.md) and [wire regressions](../tests/agent/test_proxy_request_class.py) in sync with the proxy contract.

Retire when upstream can emit the same dynamic, route-scoped header without redirect leakage, or CLIProxyAPI no longer consumes it. Unset `hermes_request_class` to disable without reverting the commit.