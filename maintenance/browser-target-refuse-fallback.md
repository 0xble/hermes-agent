# Browser target fail-closed selection

Fork patch identities: `browser-target-refuse-fallback`, `task-external-cdp-attachment`.

The active profile may pin `browser.target` and set `browser.fallback: refuse`.
Under `refuse`, every browser lane must use that one endpoint or return a structured
`browser_target_unavailable` error; it must never select Camofox, a cloud provider,
local Chromium/Lightpanda, the real-profile browser, or another CDP endpoint. The
legacy `task-external-cdp-attachment` identity on the introducing commit is retained
as an ownership alias; subsequent commits use the browser-target-specific identity.

## Contract

- **Policy resolution fails closed.** `browser_tool_cloud.browser_fallback_policy()`
  returns `allow` only when `browser.fallback` is absent or explicitly `allow`
  (case-insensitive). Any other value (typos, booleans, null), an unreadable or
  non-mapping config, or a lookup exception resolves to `refuse`.
  `browser_use_cli._cloud_refuses_pinned_target()` also resolves to `refuse` when the
  policy lookup itself raises.
- **Refuse with no target is an error, not a fallback.** `_create_session_for_key`
  routes every refusing profile through `_create_pinned_target_session`, which raises
  `BrowserTargetUnavailable` when `browser.target` is empty or unset.
  `pinned_browser_target_or_raise()` raises on an empty target.
- **The target is validated** as an absolute HTTP(S)/WS(S) URL. Unix-socket paths are
  rejected, never guessed; front a socket with a loopback HTTP(S) proxy.
- **Operator CDP is ignored under refuse.** The live `/browser connect` override,
  `BROWSER_CDP_URL` and `browser.cdp_url` are not consulted on any lane
  (`browser_tool_cdp._get_cdp_override_raw`, `browser_session_cdp.get_cdp_override_raw`).
  `browser_cdp` returns `browser_target_unavailable` for a Camofox or unset target,
  including on the `frame_id` lane.
- **Camofox only when pinned.** Under refuse, `is_camofox_mode()`,
  `get_camofox_url()` and `browser_task_cdp.use_camofox()` are true/non-empty only when
  `browser.target` is the Camofox endpoint (`browser.cloud_provider: camofox`).
  `CAMOFOX_URL`, a stored Camofox selection and task CDP attachments cannot route a
  CDP-pinned or unset profile to Camofox.
- **No engine change.** A pinned/refusing session always runs with engine `auto` and
  the Lightpanda→Chrome retry (`_run_chrome_fallback_command`,
  `_chrome_fallback_screenshot`) is skipped.
- **`browser_use_cli` (browser_exec)** runs the refuse guard first in `_route_backend`
  and `_resolve_backend_cdp`, before real-profile, local-sidecar, operator CDP, cloud
  or local-engine resolution.
- **Model-visible errors are redacted.** `browser_target_error` passes `target`,
  `reason` and `error` through `agent.redact.redact_cdp_url`.
- **`browser_navigate`** converts `BrowserTargetUnavailable` into the structured
  error payload instead of raising.
- `DEFAULT_CONFIG['browser']` is a fresh deep copy
  (`config_defaults_browser.default_browser_config()`); the only differences from the
  base defaults are the new keys `target: ""` and `fallback: "allow"`.

Contract tests: `tests/tools/test_browser_target_refuse.py`.

## Verification

Run from the checkout root with a scratch home:

```sh
h=$(mktemp -d) && HERMES_HOME="$h" env -u __HERMES_ACTIVATED \
  .venv/bin/python -m pytest tests/tools/test_browser_target_refuse.py -q; rm -rf "$h"
```

The focused tests must prove that refused targets win over cloud, local, Lightpanda
fallback, Camofox and operator-CDP selection; that an empty target, a policy lookup
failure and an unrecognized `fallback` value all fail closed; that invalid targets
produce structured errors; and that the legacy allow policy still selects local
backends.

Retire when upstream preserves the complete fail-closed target and fallback
contract. Roll back by reverting commits carrying the identities above; do not
change the target to a fallback as a recovery step.
