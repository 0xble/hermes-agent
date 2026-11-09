# Task-scoped external browser attachment

Patch identity: `task-browser-cdp`.
Contract tests: `tests/tools/test_browser_task_cdp.py`.

Trusted **in-process** plugins can temporarily route a task's ordinary
`browser_*` calls to an externally owned CDP browser without selecting a global
browser provider or changing `BROWSER_CDP_URL`, `CAMOFOX_URL`, or profile config.
This is not a plugin-host RPC capability and not a model-facing attach tool.

```python
from tools.browser_task_cdp import attach_task_browser, detach_task_browser

# Use the tool handler's runtime-injected task_id, never an argument supplied
# by the model. Resolve/authorize the endpoint in the owning plugin.
attach_task_browser(task_id, cdp_url)  # None; lazy attach, no network probe
# Subsequent normal browser_navigate/snapshot/click/type/... use this endpoint.
detached = detach_task_browser(task_id)  # bool; external browser remains alive
```

## Contract

- Task ID is mandatory, nonempty, not `default`, and not a hybrid `::local` key.
- Endpoint must be an absolute HTTP(S) discovery URL or WS(S) CDP URL. The
  plugin owns endpoint authorization; this API is full browser-control access.
- Same task/same endpoint is idempotent. Attaching over a different endpoint or
  existing agent-browser session raises `RuntimeError`; cleanup/detach first.
- Parked Camofox bindings are not removed by attach/detach. Other tasks remain
  on Camofox. Existing profile/task ownership and unique daemon names apply.
- Attachment wins over global CDP, cloud/hybrid, extension and engine routing.
  Broken endpoints fail; no retry against Camofox/local Chrome.
- Navigate, snapshot, click, type, scroll, back, press, console/eval, images and
  vision use the attached endpoint. Camofox-only tabs/upload/downloads/handoff
  return unsupported rather than act on a different browser. This seam does
  **not** add external-browser vault/secret support; plugins must not use it for
  secret entry.
- External CDP is non-local for URL/eval safety, including loopback endpoints
  (which may be forwarded). Existing allow-private policy and unconditional
  metadata floor remain effective.
- Detach releases Hermes's supervisor and verified agent-browser daemon, not the
  external browser: no `close`, `Browser.close`, or `Target.closeTarget`.
  Ordinary task cleanup/shutdown follows the same borrowed-browser rule.
- `False` detach means no matching attachment in the current profile or an
  existing protected-date quarantine prevented release; do not claim detached.
- Attach/detach must be serialized between the task's browser calls. Attachment
  is process-local, not persisted; task/inactivity cleanup releases it.
- The same external endpoint may expose shared human tabs: per-task routing is
  not tab isolation. The plugin should use a dedicated browser/window/profile
  when human or other-agent interference matters.

## Verification

`tests/tools/test_browser_task_cdp.py` invokes `registry.dispatch` with runtime
`task_id`, keeps Camofox selected, captures the final agent-browser argv, and
checks supervisor endpoint, isolation, restoration and close-free teardown.
Transport/process capture is stubbed: these are executable routing proofs, not
claims of live Donut integration or real CDP connectivity. The opt-in
`tests/integration/test_task_browser_cdp_live.py` additionally drives a fresh,
owned Playwright Chromium using real agent-browser and CDP, checks `/json/version`
after detach, and never accesses a personal browser or Donut. Run with
`HERMES_TEST_LIVE_TASK_CDP=1 .venv/bin/python -m pytest tests/integration/test_task_browser_cdp_live.py -q -s`.
