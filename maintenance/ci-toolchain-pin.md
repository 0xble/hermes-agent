# CI toolchain pinning

## Required behavior

The repository gate must use the exact `uv` and Node versions declared in
`scripts/ci/toolchain.json` when run locally, even when Homebrew or another
shell-provided tool is earlier on `PATH`. `git-guard/bin/ci-gate` remains a
wrapper: it validates the exact SHA and invokes `./bin/ci gate <sha>`; the
repository gate owns tool resolution and exact-version validation.

Local resolution reads the host mise data directory (`MISE_DATA_DIR`, or
`$HOME/.local/share/mise`). It first accepts an exact-version `uv` or Node
already found on `PATH`; otherwise it resolves the exact versioned mise install
for that tool. The checkout-owned `.ci/toolchain/node_modules/.bin` remains
ahead of mise and host directories so a provisioned npm cannot be shadowed by
Node's bundled npm. The gate also carries resolved absolute paths for `uv`, Node,
and provisioned npm into its subprocess environment. It handles mise's
`bin/<tool>` layout and uv's one-level platform archive layout. The existing
`require_tools()` check remains strict; resolution never accepts a different
version or falls back to an unvalidated tool.

If a pinned local install is absent, the gate fails before setup with the exact
missing version and a copyable `mise install <tool>@<version>` hint. GitHub
Actions keeps its existing workflow-provisioned PATH and does not use local mise
resolution. util-linux `runuser -u ci` without `-l` preserves the workflow
process environment, including `GITHUB_ACTIONS`; `portable.py` then intentionally
strips `GITHUB_ACTIONS` from the isolated lane environment while reading it from
`os.environ` only to select the hosted resolver branch. Both gate and nightly
workflows therefore use the same `runuser -u ci -- env HOME="$HOME"` form.

## Provenance and disposition

Fork patch identity: `ci-toolchain-pin`.

The independent hypothesis was frozen before upstream tracker search: the
required-version error is emitted by `portable.py` from `PINS`, which is loaded
from `scripts/ci/toolchain.json`; `git-guard` only delegates to `./bin/ci`.
The smallest correction is a local-only mise-path resolver before gate setup,
without loosening `require_tools()` or changing hosted workflow provisioning.

The repository's current origin/main pin is uv `0.12.13` and Node `26.8.2`.
The historical uv `0.9.28` value came from the earlier toolchain revision and
is not a reason to weaken the exact check. On this macOS host, the pinned uv is
stored at `uv/<version>/uv-aarch64-apple-darwin/uv`, while Node uses
`node/<version>/bin/node`.

Upstream design search on 2026-10-04 found no exact issue or pull request for
this repository-gate failure. Related upstream PR #18159 (`yoshua/path-env-fallbacks`,
open) centralizes broad PATH fallbacks for doctor/browser subprocesses, including
mise shims, but it intentionally does not provide exact repository-pin
resolution and does not cover this portable CI gate. It is design context only,
not an adopted implementation.

No upstream contribution is claimed. Retire this fork patch when a released
upstream or replacement fork CI contract resolves these exact local pins,
preserves the missing-install failure and exact-version check, and leaves
GitHub-hosted provisioning equivalent. Rollback is a revert of the commit that
adds the resolver, tests, and this record; it restores the prior PATH-dependent
local behavior without changing toolchain pins.

## Verification

Regression tests in `scripts/ci/tests/test_portable.py` cover:

- nested mise uv layout plus Node `bin` layout winning over an incompatible host PATH;
- `test_local_environment_accepts_exact_path_tools_without_mise_install`;
- a fresh setup provisioning pinned npm after initial resolution, then keeping it ahead of mise's bundled npm;
- a missing pinned uv producing the exact install hint; and
- GitHub Actions retaining host/workflow tool resolution.

Focused command:

```text
python3 -m unittest scripts.ci.tests.test_portable.PortableGateTests.test_local_environment_resolves_pinned_uv_and_node_from_mise scripts.ci.tests.test_portable.PortableGateTests.test_local_environment_accepts_exact_path_tools_without_mise_install scripts.ci.tests.test_portable.PortableGateTests.test_setup_re_resolves_pinned_npm_after_provisioning scripts.ci.tests.test_portable.PortableGateTests.test_local_environment_fails_with_install_hint_when_pinned_mise_tool_is_missing scripts.ci.tests.test_portable.PortableGateTests.test_github_environment_keeps_host_toolchain_resolution
```

The focused regression was red before the implementation and green afterward.
After `./bin/ci setup` provisioned the pinned toolchain/dependencies, the full
portable unit module passed (`.venv/bin/python -m unittest scripts.ci.tests.test_portable`;
36 tests, exit 0). A preliminary system-Python run was invalid because it lacked
`ruamel` and the required SQLite constants; it is not counted as candidate
verification.
