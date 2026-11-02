# Startup Capability Reads

Fork patch identity: `fork-ci-reliability`.

Session creation builds browser descriptions and vision/video availability gates before
inference. These are metadata reads, not requests to route an image. Pass cache-only
capability policy through the native-image gate and auxiliary vision auto-routing so
models.dev DNS, HTTP, or its background refresh lock cannot hold session creation.
Cached catalog data, provider model declarations, and explicit vision overrides still
apply. Cache-only image capability checks also omit local server probes. Existing
catalog warmers may populate the cache independently. Actual image routing and explicit
interactive lookups retain their existing network-enabled defaults and text-only guard.

`tests/gateway/test_startup_capabilities_offline.py` starts the real unified gateway,
holds models.dev DNS indefinitely in the existing background refresher, and creates a
persisted session over its ticketed WebSocket before DNS can finish. The browser-use
schema and vision/video availability gates participate in the normal tool inventory.
`tests/agent/test_vision_capability_network_policy.py` covers cached answers, config
overrides, endpoint-probe omission, and unchanged runtime discovery. Also run the
browser/vision consumer suites and native Codex app-server fault scenarios.

Retire this adaptation when accepted upstream startup metadata reads preserve the same
cache-only boundary. Rollback restores synchronous discovery in the capability gates
and removes the regressions, without changing runtime image input mode defaults.
