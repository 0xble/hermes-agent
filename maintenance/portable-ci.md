# Portable contributor CI

`bin/ci` sets up checkout-owned dependencies and runs the full source gate on
its actual host. It requires no mise, personal Hermes installation, credentials,
or publisher service. `bin/ci setup` only installs dependencies. `bin/ci check`
checks an existing setup. `bin/ci list` describes the seven default lanes.
`bin/ci check --lane node` is explicitly partial evidence, never a full pass.

The desktop E2E workflow installs the same Python 3.14.7 it selects for sync.
Desktop typechecking includes test sources, so native filesystem test helpers
must import the operations they call even when the test runs only on Linux.
Update receipt tests bind ContextVars through their scope and isolate ownership
lookup from the host's managed test-interpreter installation. Production ownership
resolution has its separate invariant test. These corrections remain within
`fork-ci-reliability`.
The native Telegram cancel-before-entry test awaits its task callback rendezvous
before asserting admission release. PTB stop and an already completed Task do not
by themselves drain registered callbacks. The production admission code is unchanged.

The full gate runs all selected lanes and returns nonzero if any fails. Within
static checks and container lint, independent commands also aggregate failures.
A setup failure stops the gate because dependencies are not qualified. The
standalone runner owns timeouts, cancellation, process-group cleanup, logs and
publication. A timeout or killed run is never a passing result.

The portable Python, E2E, and native OS lanes disable automatic file retries.
A first-attempt test failure fails CI even if a retry might pass. Direct
interactive use of `scripts/run_tests.sh` retains its default retry behavior.

Dependency-bound and plugin-admission checks inspect current working files by
default, including newly added catalog entries. Their explicit `--head` option
checks a committed revision instead. Plugin admission fetches exact pins and
executes dependency validation, so run catalog changes in a disposable,
credentialless environment. The required runner already provides that isolation.

Mutable state lives in the current worktree: `.venv`, `node_modules`,
`website/node_modules`, and ignored `.ci/` caches and Cargo targets. Temporary
homes live outside the checkout in invocation-owned system temporary directories.
When Linux makes the normal disk-backed Python scratch path read-only, Python
fixtures use a unique directory under the runner's original disk-backed HOME,
outside the isolated child HOME. Temporary parents with Git or Node dependency
ancestry are rejected. Only invocation-owned directories are cleaned. A kernel lock prevents concurrent setup/check in that checkout and is
released automatically on process exit. Checkouts never use another checkout's
venv or personal pytest plugin. Credential and test-selection environment
variables are absent from the child environment. Installed rustup users share
only the resolved toolchain executable directory. Cargo configuration/cache is
checkout-owned. Setup/check detect any edits to pre-existing source files and
fail without overwriting the contributor's work.

Docs generators run in a disposable copy of source, including uncommitted
contributor edits. Parity compares file content and the complete added/deleted
file set under `website/docs`, `website/sidebars.ts`, and `website/i18n`.
The snapshot also runs links, diagram lint and the English build. Generated
skills dashboard data is built there, matching the old workflow's parity scope.
No generator or build writes into the contributor's website source.

The public fork runs ten hosted Linux Python/E2E shards on both PR and nightly. All ordinary files under `tests` (excluding integration/docker), plus PR-safe `tests/e2e`, are partitioned exactly once. `scripts/ci/python_shard_timings.json` records files taking at least five seconds in the first eight-shard trial ([run 36456796832](https://github.com/0xble/hermes-agent/actions/runs/36456796832)); shorter and newly added files get a size estimate of 0.7–4.9 seconds at 3 KB/s. Largest estimated duration first, then path and shard-index ties, makes assignment identical across independently provisioned jobs; the untracked runner `test_durations.json` never affects it. Refresh the checked-in table from a complete hosted run when file timings materially drift, rather than from a local runner cache. `scripts/run_tests.sh` still gives each file its own process and `--file-retries 0` in CI. Upgrade E2E retains the nightly-only upstream-tag fetch and 900-second per-file timeout; the SQLite torture chamber also stays nightly-only until its known FTS corruption and kill9 failures are fixed. Known-slow ordinary files listed in `FILE_TIMEOUTS` run as their own invocation under a committed larger bound (see [ci-slow-file-timeouts.md](ci-slow-file-timeouts.md)); every other file keeps the 300-second default. Static and bounded Node remain a separate gate job. The PR `qualification` depends on both it and the entire ten-shard matrix: twelve hosted jobs total. GitHub Free has 20 concurrent hosted jobs across the account, so two PR gates plus a nightly can queue; do not enlarge the PR matrix without measuring that contention. Nightly's qualification additionally depends on broad Linux, native macOS/Windows and desktop-core. Each Linux job uses the same pinned container with `--init`, installs its pinned tools as root, then runs the exact-SHA profile as the non-root `ci` user. Python shards checkout only the exact head at depth one; they need no ancestor or remote branch. Static/Node instead shallow-checks the head bloblessly and unshallows only its history plus `origin/main` (needed by attribution and history policy), avoiding checkout's all-branch full-clone fetch. Nightly's broad Linux job does the same for previous-release checks. This matrix executes full ordinary Linux Python and PR-safe E2E on every same-repository PR, not just the previous four focused Python files. Native, desktop-core, full Node, docs, Rust, container lint, upgrade and torture remain nightly-only.

## Tools and Linux image

Exact CLI versions are declared in `scripts/ci/toolchain.json`. Python uses the
repository's Python 3.14 line with a fixed CI patch. Root Python and Node dependencies
use their existing locks. Docs Python tools are ascii-guard 2.3.0 and ruamel.yaml
0.18.16, matching the repository YAML reader. The bootstrap application now tracks Cargo.lock and runs `cargo test
--locked --lib`. The first lock captures the existing manifest's 553-package
resolution, without changing manifest dependency bounds. Linux arm64
qualification compiled the locked graph and passed all 68 library tests.
Other platforms still require native qualification.

`ci/Dockerfile` supplies amd64/arm64 Linux prerequisites. Its Node base is pinned
by the multiarch manifest digest. Rust, uv, ripgrep, hadolint and shellcheck
archives have exact versions and SHA-256 hashes in `ci/linux-artifacts.json`.
Rust hashes come from the official release channel manifest, the other hashes
from release checksums, except shellcheck whose downloaded release archive was
hashed during pinning. A trusted builder must publish and configure the final
image digest. Debian package repositories remain moving inputs to image builds,
so the recipe alone is not a bit-for-bit image reproduction guarantee.

Build from the repository root with `docker build -f ci/Dockerfile .`.
`ci/Dockerfile.dockerignore` sends only build inputs, not the source/dependencies.
The image requires no host Docker socket. Root is read-only during jobs and the
runner provides writable checkout, home and scratch mounts. Linux Node checks
require Xvfb and Electron libraries and fail if those are unavailable. Rust
requires Tauri's WebKit/GTK development libraries. Native macOS contributors
need the matching tools and Xcode prerequisites. Windows-specific execution
remains a separate native Windows lane, not Linux emulation.

Default concurrency is four Python files, two Node checks, and two Cargo jobs.
Linux arm64 qualification uses a 3-CPU, 6-GiB worker in a 4-CPU, 8-GiB VM
with a 30-GiB disk. One complete seven-lane run took about 52 minutes without
an OOM kill, although its Python failures made the overall result fail.
A subsequent run reached a 5.47-GiB cgroup memory peak during Python tests.
These measurements establish an exercised budget, not a minimum requirement
or a passing source gate. Leave disk space for dependencies, caches and Rust
build output. Other platforms and simultaneous full gates need their own
resource qualification.

## Coverage allocation before workflow removal

The candidate removes all 36 inherited workflow files. Remote enforcement stays
in place until replacement qualification and cutover. This table records
their retirement disposition, not proof that each replacement has passed.
At migration baseline `4963f108`, the fork's required `local-ci/full` ran only
the nine nonrelease Node checks. The seven source lanes expand that enforced
baseline. Inherited native jobs are separate platform evidence, not additional
required fork statuses. Linux success does not qualify macOS or Windows.

| Existing workflow | Allocation |
| --- | --- |
| `ci.yaml` | `bin/ci` aggregates seven blocking source lanes. Trusted publisher owns status and cancellation semantics. |
| `tests.yml` | `python`: canonical `scripts/run_tests.sh tests`. `e2e`: canonical `scripts/run_tests.sh` over `tests/e2e`, with `tests/e2e/core/upgrade` in its own run under a 900 s per-file bound (upstream runs it as a separate `e2e-upgrade` job; real N-1 -> HEAD updates exceed the 300 s default). Profile-plugin source is maintained outside this checkout. Integration and Docker directories stay excluded from the ordinary Python lane. |
| `tests-os.yml` | Hosted nightly `native` matrix on macOS and Windows, using `bin/ci nightly-native <sha>` and the existing marker selector/canonical runner. Require the actual OS and nonzero selection. |
| `lint.yml` | `static`: blocking ruff, Windows-footgun, compatibility-pointer, temporary-path and OS-marker checks. Advisory ruff/ty comparison stays a separate advisory review lane. |
| `js-tests.yml` | `node`: nine nonrelease workspace units plus behavioral runner tests. Signed desktop packaging remains a release lane. |
| `installer-tests.yml` | Native Windows maintainer qualification under PowerShell 5.1 and 7, using the three existing installer scripts below. |
| `rust-tests.yml` | `rust`: bootstrap installer `cargo test --locked --lib` on the actual host. Unix pipe-drain tests require a Unix host. Windows update self-tests remain native. |
| `docs-site-checks.yml` | `docs`: metadata extraction, generation parity, links, diagrams, English Docusaurus build. |
| `history-check.yml` | `static`: full history and common ancestor with `origin/main`. |
| `contributor-check.yml` | `static`: new nonmerge author emails since merge-base, contributor files and legacy author map, existing bot/vendor exemptions. No GNU grep dependency. |
| `uv-lockfile-check.yml` | `static`: `uv lock --check`, plus setup's `uv sync --locked`. |
| `infographic-check.yml` | `static`: tracked infographic-like raster directory paths. Preserves the actual workflow rule, including its lack of the exemptions mentioned only in comments. |
| `profile-artifact-check.yml` | `static`: profile archive boundary. Advisory scope-pattern diagnostics remain review evidence. |
| `case-collision-check.yml` | `static`: case-collision checker. |
| `fork-patch-check.yml` | `static`: `check_fork_patches.py --repo . --source-only`. Full history and maintained trailer floors remain required. |
| `docker-lint.yml` | `container-lint`: hadolint warning threshold and docker shellcheck error threshold. No claim of image execution. |
| `plugin-catalog-ci.yml` | `static`: complete structural validation plus changed-entry admission at exact pinned SHAs, dependency validation and self-updater checks. The runner supplies the disposable integration sandbox. |
| `nix.yml` | Residual independent advisory `nix flake check --print-build-logs` with Nix available. |
| `lockfile-diff.yml` | Residual dependency-diff review evidence, not executable correctness proof. |
| `supply-chain-audit.yml` | Preserve blocking added-dependency bounds in static validation. Critical pattern findings require ordinary maintainer review. Retire the `ci-reviewed` label protocol. |
| `review-labels.yml` | Ordinary maintainer review replaces the custom label protocol. Review remains outside the untrusted contributor gate and needs no contributor credentials. |
| `docker.yml` | Retire upstream-owner-only image build/publish automation. Explicit image or container changes still need applicable `tests/docker` qualification in an isolated container-capable environment. |
| `install-e2e.yml` | Residual installer integration orchestration across actual operating systems. |
| `install-e2e-run.yml` | Residual native Linux installation/update integration. |
| `install-e2e-macos-run.yml` | Residual native macOS installation/update integration. |
| `install-e2e-windows-run.yml` | Residual native Windows installation/update integration. |
| `windows-venv-e2e.yml` | Preserve the six native Windows live tests below. The old workflow was on-demand on `wine2e/**`, not normal PR coverage. |
| `e2e-desktop.yml` | Already disabled upstream (`if: false`). Remains explicitly disabled, with no newly covered desktop Playwright claim. |
| `e2e-desktop-core.yml` | Called by `nightly.yml` (`desktop-core`, required by nightly `qualification`) and runnable on demand. The deterministic core suite is not part of the PR gate. |
| `deploy-site.yml` | Retire upstream-owner-only site publication. A source build does not publish the site. |
| `skills-index.yml` | Retire upstream-owner-only scheduled refresh/publication. |
| `skills-index-freshness.yml` | Retire upstream-owner-only freshness monitor. |
| `osv-scanner.yml` | Optional maintainer vulnerability scan over the five lockfile inputs below. Findings are advisory. No replacement schedule or automatic publication is implied. |
| `ci-review-comment.yml` | Trusted review/status presentation, outside contributor gate. |
| `publish-e2e-evidence.yml` | Trusted evidence publication, separate from test execution. |
| `label-rerun.yml` | Retire with the custom review-label protocol. |
| `js-autofix.yml` | Optional authored autofix workflow, not a correctness gate or authority for untrusted source to push. |

## Native and release qualification

`nightly.yml` runs `bin/ci nightly-native <sha>` at the exact scheduled or
dispatched SHA on hosted `macos-26` (arm64) and `windows-2025-vs2026`; both matrix jobs
must pass for nightly `qualification`. The PR gate remains Linux-only. The
native profile performs setup and selects files covering `macos` or
`windows` through `scripts/ci/list_os_marked_tests.py`, then runs the
canonical Python harness with that marker and `not integration`. Empty selection
is a failure. Windows also runs the existing long-path, Node-compatibility and
uv-shim installer scripts under both `powershell` 5.1 and `pwsh` 7. Linux cannot
substitute for these checks. Earlier local macOS arm64 evidence covered 94 tests
across 36 files with Python 3.11.14; it does not establish a result for the
current nightly candidate or for hosted Windows.

For Windows process-lifecycle changes, the former on-demand `wine2e/**` selection
remains available through the canonical harness on an isolated Windows machine:

```sh
bash scripts/run_tests.sh -j 4 \
  tests/hermes_cli/test_venv_holder_windows_live.py \
  tests/hermes_cli/test_taskkill_identity_windows_live.py \
  tests/hermes_cli/test_git_trampoline_windows_live.py \
  tests/hermes_cli/test_managed_uv_windows_cutover.py \
  tests/gateway/test_telegram_closewait_windows_live.py \
  tests/tools/test_process_registry_windows_live.py \
  -o addopts= -v -p no:cacheprovider
```

Installer release qualification uses the existing drivers under `tests/install/`:
`installer-script-e2e.sh`, `macos-desktop-e2e.sh`, and `windows-e2e.ps1`.
Their artifact, GUI and platform prerequisites remain required. Run them in a
disposable native environment, never against an installed personal Hermes runtime.
The inherited installer workflows ran on schedules, release tags or manual
dispatch. Removing them does not turn the source gate into installation proof
or authorize a release. Signed desktop packaging remains a separate release gate.

Optional maintainer checks include `nix flake check --print-build-logs` and an OSV
scan of `uv.lock`, root and website `package-lock.json`,
`plugins/platforms/photon/sidecar/package-lock.json`, and
`scripts/whatsapp-bridge/package-lock.json`. Vulnerability findings are advisory
review inputs, not a fabricated passing scan or an automatic dependency update.

## Provenance and qualification

This extends `fork-ci-reliability`, not a new fork identity. It preserves
`ci-gate-cancelled-blocks` and the maintenance trailer contract. The governing
upstream guidance was inspected at
`439eb0395eed5025139ee917526f31e2d161699e`, without syncing a release.
Upstream PR 115214's output-drain fix is already present in the moved workspace
runner and preserved. Invalid concurrency was separately reproduced as false
success before adding behavioral regression tests and strict argument handling.

The bounded smoke manifest remains partial. It is never substituted for these
full Python roots. Historical outcomes in `fork-ci.md` remain historical, not
current qualification. No installed Hermes runtime is checked or promoted here.

The documentation environment bootstrap preserves its existing checkout-local venv with `uv venv --allow-existing`, so running setup before gate or repeating gate does not fail merely because `.ci/docs-venv` exists. Pinned documentation packages are installed on every setup.


## Pre-Tip Checkpoint Adaptation (2026-09-30)

The explicit checkpoint exception imports upstream Python 3.14 and the native PM tool owner. Native OS selection lists files covering the actual host, then selects `platforms` tests. Optional memory SDK integration is the `hindsight` extra. The separate `hindsight-embedded` extra opts into the embedded server and is not inferred from incidental shared transitive distributions. Immutable staging preserves declared extras and all active plugin entry points, and validates enabled plugin imports before activation.

Release, signing, store publishing and channel promotion workflows remain removed under the fork workflow policy. Tests whose entire owner disappeared are retired with that owner. Generic archive/link, SHA admission, ancestry and receipt security tests remain. Workflow policy coverage remains in `scripts/ci/tests/test_portable.py` and `tests/ci/test_workflow_job_graph.py`. The resumable reconciliation evidence records each removed test owner and partial retirement separately.

## Native Checkpoint Fixture Boundaries

Release-publication corruption tests call the actual `scripts.bundles.release_artifacts promote` and builds-table CLI owners. Termux build tests call the native build driver directly. Neither depends on a deleted Actions workflow. Archive fixtures force-add their declared local wheel directory, so a contributor's global Git ignore cannot silently remove the fixture dependency from the archived source. Python payload signing requires both a Darwin host and a Darwin target.

Host-specific cases use one native `platforms` marker. Real profile and adapter constructors, explicit selected-payload ownership, an empty completion-parent venv, interpreter-reported base prefixes, and event-controlled probes replace obsolete environment assumptions. Concurrent local transfer fixtures use a threaded HTTP server, preserving the production resolver's IPv4/IPv6 connection behavior. Memory tests exercise actual provider SDK admission and the Hindsight no-install hot-path contract instead of removed dependency bookkeeping. These are fixture adaptations, not waivers of the portable gate or native qualification.

The startup-gate config read uses the repository-required `utf-8-sig` decoding policy. Its safe YAML parser already accepts a BOM, so this correction is a source-policy alignment, not a reproduced runtime decoding failure. The existing public A→B→A startup invariant exercises BOM and BOM-less configurations. Mutable startup-gate fixtures use upstream's `hermes_yaml` owner rather than the removed PyYAML dependency. The historical frozen outbox fixture remains unchanged and its child receives the actual `pyyaml==6.0.3` dependency from that release in a disposable local dependency directory. It does not restore PyYAML to the current runtime or substitute a fake module.
