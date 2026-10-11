# Secret-safe provider diagnostics

Load when changing provider HTTP diagnostics, the standalone probe, or its
credential and logging boundary. Fork patch identity: `io-safe-provider-probe`.

The Io integrity work needs a single selected-endpoint check without putting a
credential into a shell command or exposing a provider's reflected error body.
Existing `hermes doctor` connectivity checks span multiple providers and SDKs;
its `--live` checks cover tool backends, not this narrow custom endpoint check.
The standalone script therefore reuses the runtime's read-only named-custom
provider lookup, secret reader, HTTP client and unconditional egress redactor
without running either doctor suite. No new dependency or user configuration
is required. `hermes_cli.runtime_provider._get_named_custom_provider` is the
source for the endpoint and resolved credential: keyed `providers` entries
precede legacy `custom_providers`, and a keyed provider's `key_env` credential
precedes its inline key. Process environment precedes the selected home's
`.env`, loaded before runtime lookup. The full runtime builder is deliberately
not called because credential-pool loading can seed/persist auth state and
selection can refresh credentials; persisted selected-provider pools fail
closed instead. The limited no-pool key fallback uses the runtime key-env reader
and cites the runtime candidate order in the probe.

The supported surface is an explicitly configured OpenAI-compatible custom
endpoint selected by `model.provider` (`providers`, legacy `custom_providers`,
or bare `custom` with `model.base_url`). Credentials come from `api_key`,
`key_env`/`api_key_env` in process environment, or the selected home's `.env`.
Only `/models` is requested: no inference, credential refresh/commands, fallback,
redirects, proxy-environment routing, or response-body inspection. Unsupported
transports and missing credentials fail closed. HTTPS is required except for
loopback HTTP. Output is status-only and force-redacted; dependency logging and
incidental configuration output are suppressed in this dedicated process.

Verify with `scripts/run_tests.sh tests/scripts/test_probe_provider.py`.
The local stub observes the actual child argv and Authorization header, returns
reflected opaque and vendor-shaped tokens on success, errors and redirects,
and checks stdout, stderr and DEBUG-level file logging with redaction disabled
in configuration. A disconnect and unsafe configurations are covered too. The
runtime-parity regressions compare mixed keyed/legacy entries and conflicting
inline/env credentials against the runtime's no-pool configured-provider path,
then assert the actual request path and Authorization header. Pool rejection
also verifies that auth state is left unchanged.

Upstream preflight: main `79b45d1647f6b5680768d851c4c406e8cc550a1e`
and read-only issue/PR searches found no equivalent single-endpoint secret-safe
helper. Retire when upstream ships the same narrow boundary and these tests pass
against it. Rollback reverts the commit carrying this identity; no stored-state
migration or runtime restart is involved.
