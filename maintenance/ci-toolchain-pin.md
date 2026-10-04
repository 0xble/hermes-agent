# CI toolchain pinning

## Required behavior

The repository gate must use the exact `uv` and Node versions declared in
`scripts/ci/toolchain.json` when run locally, even when Homebrew or another
shell-provided tool is earlier on `PATH`. `git-guard/bin/ci-gate` remains a
wrapper: it validates the exact SHA and invokes `./bin/ci gate <sha>`; the
repository gate owns tool resolution and exact-version validation.

Local resolution reads the host mise data directory (`MISE_DATA_DIR`, or
`$HOME/.local/share/mise`) and prepends the exact versioned install directories
for `uv` and `node`. The gate also carries their resolved absolute paths into
its subprocess environment, so checkout-owned or host PATH entries cannot
shadow the exact pins; the provisioned npm package is similarly selected when
present. It handles mise's `bin/<tool>` layout and uv's one-level platform
archive layout. The existing `require_tools()` check remains strict;
resolution never accepts a different version or falls back to `PATH`.

If a pinned local install is absent, the gate fails before setup with the exact
missing version and a copyable `mise install <tool>@<version>` hint. GitHub
Actions keeps its existing workflow-provisioned PATH and does not use local
mise resolution. The two existing `runuser` gate invocations pass
`GITHUB_ACTIONS=true` explicitly because `runuser -- env HOME=...` intentionally
starts with a minimal environment; this preserves the hosted-runner branch of
the resolver without changing the runner's toolchain or test lanes.

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
- a missing pinned uv producing the exact install hint; and
- GitHub Actions retaining host/workflow tool resolution.

Focused command:

```text
python3 -m unittest scripts.ci.tests.test_portable.PortableGateTests.test_local_environment_resolves_pinned_uv_and_node_from_mise scripts.ci.tests.test_portable.PortableGateTests.test_local_environment_fails_with_install_hint_when_pinned_mise_tool_is_missing scripts.ci.tests.test_portable.PortableGateTests.test_github_environment_keeps_host_toolchain_resolution
```

The focused regression was red before the implementation and green afterward.
After `./bin/ci setup` provisioned the pinned toolchain/dependencies, the full
portable unit module passed (`.venv/bin/python -m unittest scripts.ci.tests.test_portable`;
33 tests, exit 0). A preliminary system-Python run was invalid because it lacked
`ruamel` and the required SQLite constants; it is not counted as candidate
verification.
