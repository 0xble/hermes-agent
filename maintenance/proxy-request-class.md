# Proxy request class

Fork patch identity: `proxy-request-class`. This fork-only patch supplies the CLIProxyAPI scheduling contract; a static `extra_headers` entry cannot distinguish human and background calls. No upstream equivalent was identified at `3371c03df62106a4c93a545787a471e328883d08`. Upstream [#69233](https://github.com/NousResearch/hermes-agent/pull/69233) addresses named identity for static headers, not this dynamic classification.

Read [turn-origin provenance](turn-origin-provenance.md) when adapting ingress. Preserve named provider identity across auxiliary resolution and client reuse: two entries may share a URL and credential but have different opt-ins. The scheduling hint is not authorization and does not change local admission, health, or fallback policy. Keep the [provider setup documentation](../website/docs/user-guide/configuring-models.md) and [wire regressions](../tests/agent/test_proxy_request_class.py) in sync with the proxy contract.

In-turn compression follows the parent turn’s class because it blocks the reply; standalone compression is background. Title upgrades remain background even when they inherit the profile and route context of an interactive turn. A human steer or interrupt carried over from a background turn runs interactively.

Retire when upstream can emit the same dynamic, route-scoped header without redirect leakage, or CLIProxyAPI no longer consumes it. Unset `hermes_request_class` to disable without reverting the commit.

Contract tests: `tests/agent/test_proxy_request_class.py`.

## Verification

Run `scripts/run_tests.sh tests/agent/test_proxy_request_class.py tests/gateway/test_vision_preprocess.py tests/gateway/test_local_viewer_events_submit.py tests/hermes_cli/test_gateway_agent.py tests/hermes_cli/test_quiet_turn_author.py tests/tui_gateway/test_quiet_wakeup_silence.py tests/acp_adapter/test_server.py`. Check that unknown origins and user-facing calls stay interactive, only opted-in routes receive the header, and redirects do not forward it. Qualify the exact PR head with the Repository gate.

## Latest qualification

- Exact-head qualification: `scripts/check --only health` passed with 0 blocking and 0 advisory findings; the final SHA and gate result are recorded in the PR body.
- Exact-head local gate returned `GATE: PASS` with a case-sensitive `TMPDIR`; hosted run 44525535848 remains the PR gate reference.
