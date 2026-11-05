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

`model_tools._rewrite_browser_exec` must pass `allow_network=False` to
`_should_use_native_vision_fast_path`, matching the browser-use description header.
The upstream session-availability rule remains: hide browser_exec without terminal;
name vision_analyze only when that tool is present and native vision is not selected.

`tests/gateway/test_startup_capabilities_offline.py` starts the real unified gateway,
holds models.dev DNS indefinitely in the existing background refresher, and creates a
persisted session over its ticketed WebSocket before DNS can finish. It then submits
a prompt and waits for the real agent's dynamic tool-schema assembly to finish with
terminal, vision_analyze, and browser_exec present; session.create alone returns before
that work. `tests/tools/test_browser_exec_startup_offline.py` drives the real rewrite
behind blocked models.dev DNS and checks the session-availability/native-vision matrix.
`tests/tools/test_model_tools.py` preserves the upstream availability rule while also
requiring cache-only native-vision calls during dynamic schema assembly.
`tests/agent/test_vision_capability_network_policy.py` covers cached answers, config
overrides, endpoint-probe omission, and unchanged runtime discovery. Also run the
browser/vision consumer suites and native Codex app-server fault scenarios.

Retire this adaptation when accepted upstream startup metadata reads preserve the same
cache-only boundary. Rollback restores synchronous discovery in the capability gates
and removes the regressions, without changing runtime image input mode defaults.
