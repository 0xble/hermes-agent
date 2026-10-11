# Secret-safe provider diagnostics

Load when changing provider HTTP diagnostics, the standalone probe, or its
credential and logging boundary. Fork patch identity: `io-safe-provider-probe`.

The Io integrity work needs a selected-endpoint check without putting a
credential into a shell command or exposing a provider's reflected error body.
Existing doctor checks span multiple providers and SDKs. This standalone
helper is deliberately not another runtime resolver: admission fails closed
where routing or credential precedence could be ambiguous.

## Accepted inputs

Run `.venv/bin/python scripts/probe_provider.py <key>` with exactly one literal
`providers.<key>` config key matching `[a-z][a-z0-9_-]*`. It does not select from
`model.provider`, display names, `custom:<name>` aliases, bare `custom`, or
legacy-only `custom_providers`. Add a uniquely named keyed entry when needed.
The entry must be enabled, have a usable configured credential and an
OpenAI-compatible URL/transport, and have no overlapping declarations or pools.

The runtime's `_match_new_style_provider` owns keyed-entry URL and credential
selection (`api`/`url`/`base_url`, `key_env`/`api_key_env` before inline `api_key`).
Process environment precedes the selected home's `.env`. A declared credential
environment variable is allowed; routing overrides are not. Nonempty
`*_BASE_URL`, `HERMES_INFERENCE_PROVIDER`, `CUSTOM_API_KEY`, `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`, `OLLAMA_API_KEY`, and other `*_API_KEY` variables are
refused, except the entry's declared credential variable when it is not itself
a runtime override. The runtime's host-gated/host-derived key helper also checks
for overrides, including one that coincides with the declared variable.

## Refusals and safety boundary

- FailedConfigRead, disabled/missing entries, implicit selection, and built-in
  provider keys are unsupported.
- Any other keyed or legacy declaration sharing the key, display name, or a
  `custom:` alias is refused, even an identical duplicate. Do not reconstruct
  the runtime's precedence or deduplicated picker view.
- Persisted pools for the provider key, display name, `custom:<key>`,
  `custom:<name>`, or bare `custom` are refused. Also guard the runtime's pool
  candidate helper for each identity, including URL-matched candidates. Inspect
  both profile/global auth files directly without recovery, locks, or migrations;
  malformed/unreadable stores are refused, never treated as empty or backed up.
  Never load/select a credential pool.
- `key_cmd`, extra authentication headers, invalid/unsupported transports,
  codex app-server overlays, native Gemini/Vertex, Bedrock Mantle, Actual URL
  rewriting, and model-dependent OpenCode routing are refused. Undeclared modes
  use the runtime URL detector; only chat-completions/Responses wires pass.
- Missing/unusable keys, CR/LF in credentials, URL userinfo, query/fragment,
  credential-bearing URLs, and non-HTTPS endpoints outside loopback are refused.

Only one `/models` GET is permitted: no inference, refresh/commands, fallback,
redirects, proxy-environment routing, or response-body inspection. Output is
status-only and force-redacted. Dependency logging and incidental configuration
output are suppressed in this dedicated process. The full runtime builder is
not called: pool loading can write auth state, selection can refresh, and model
resolution can make auto-detection requests. Read-only config loading retains
its existing last-known-good bookkeeping, but a fallback config cannot be probed.

## Verification and retirement

Run through the existing heavy slot:
`heavy-slot --label io-probe-redesign -- scripts/run_tests.sh tests/scripts/test_probe_provider.py`.
The HTTP stub observes actual child argv and Authorization headers, reflects
opaque/vendor-shaped tokens in status reasons and bodies, and checks stdout,
stderr and DEBUG logs with redaction disabled. Every accepted test fixture is
compared with `resolve_runtime_provider` with only the pool rung disabled.
Refusal fixtures count TCP connections and cover the two round-4 cases (bare
custom plus CUSTOM_BASE_URL; display/legacy collisions plus a keyed pool), all
candidate pool identities, environment overrides, aliases and native transports.
Pool checks also prove auth state is unchanged; accepted controls must connect.

Upstream preflight: main `79b45d1647f6b5680768d851c4c406e8cc550a1e`
and the existing read-only issue/PR searches found no equivalent single-endpoint
secret-safe helper. This redesign retains that fork-only helper/rule scope; no
upstream write, runtime update or live provider probe is involved. Retire when
upstream ships the same narrowed boundary and these tests pass against it.
Rollback reverts the commit carrying this identity; no stored-state migration
or runtime restart is involved.
