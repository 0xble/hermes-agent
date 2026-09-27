# Browser upload

Load this unit when changing `browser_upload`, Camofox tab actions, or the `browser.camofox.uploads_dir` setting.

## Required behavior

`browser_upload` attaches local files to the page's upload control through Camofox's `POST /tabs/{tabId}/upload`. That route answers the page's own file chooser or mounted `input[type=file]`, including inside cross-origin iframes, with no OS dialog. The tool is registered only in Camofox mode.

- **Path checks:** each path is resolved and checked with `agent.file_safety.get_read_block_error` before anything is sent. Credential stores, `.env` files and other denied paths are refused, and a missing file never reaches the server.
- **Staging:** Camofox accepts only files under its uploads root. The root is `CAMOFOX_UPLOADS_DIR`, then `browser.camofox.uploads_dir`, then Camofox's default `~/.camofox/uploads`. A file outside the root is copied into `<root>/hermes/<sha256-16>-<name>` and the copy is kept, because pages may read the file after the attach returns. A file already inside the root is used in place.
- **Chooser wait:** the request leaves Camofox's own chooser wait (12 s) alone. Camofox aborts every tab action at 30 s, and its upload route polls for a mounted input for nearly the whole wait before consuming the chooser. A longer wait turns a successful attach into a 500.
- **Result:** the result names the attached files and the route (`direct_input`, `panel_input`, `filechooser`), and tells the agent to snapshot, save and read back.

## Provenance and patches

- Fork patch identity: `browser-upload`.
- Motivated by the 2026-09-27 GHL calendar-logo run. The agent found the Camofox route only by reading the server source, and failed first with page scripting and mouse clicks on a cross-origin iframe.
- Upstream Hermes has no browser file-attach tool. A Camofox-mode, `check_fn`-gated core browser tool is the smallest surface that makes the existing server route reachable. It is the same rung as `browser_handoff`.

## Verification

`scripts/run_tests.sh tests/tools/test_browser_upload.py tests/tools/test_browser_camofox.py tests/tools/test_model_tools.py tests/test_toolsets.py tests/tools/test_registry.py tests/agent/test_tool_guardrails.py tests/hermes_cli/test_config.py`. The live check runs a local outer page with an upload button in an iframe on a second origin, and calls `camofox_upload` with the selector `iframe[src*="<port>"] >> internal:control=enter-frame >> #up`. The frame must report the same name, size and hash as the staged file.

## Retirement and rollback

Retire if upstream ships an equivalent browser attach tool that covers Camofox. To roll back, revert the tool, schema, toolset entry, config default and tests. Staged copies under `<uploads root>/hermes/` are disposable.
