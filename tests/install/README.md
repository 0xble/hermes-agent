# Install and Update E2E Tests

These tests answer one question: can a user on a released version get to this commit?

Each test leg installs an old released version, then updates it to HEAD. The install and the update run the real user surfaces. The legs do not use mocks and do not use headless proxies of GUI flows.

## The layers

The support matrix is declared by `scripts/sandbox/generate-e2e-matrix.mjs`.
The retained drivers execute supported install/update pairs:

- `tests/install/installer-script-e2e.sh`: POSIX script installs.
- `tests/install/macos-desktop-e2e.sh`: macOS desktop installs.
- `tests/install/windows-e2e.ps1`: Windows installs.

Run these separately from `bin/ci` as release qualification in disposable native
VMs. Select explicit release refs and supported methods from each driver's help
and implementation. Declaring a matrix pair does not implement its driver.
There is no hosted scheduler or automatic release qualification in this fork.

## The isolation trick

The drivers do not touch the network for git operations. Each driver makes a bare clone of the checkout at `serve.git`. Then it points every git process at this clone. The mechanism is a driver-owned `GIT_CONFIG_GLOBAL` file with `url.<file://serve.git>.insteadOf` rewrites for both canonical repository URLs.

The driver parks the `main` branch of `serve.git` at the old release. The installer runs and lands on the old release. Then the driver moves `main` to HEAD. An update becomes available in the same way that it does for a real user.

The installer script is not downloaded. The install leg runs the copy from the old git ref. This is the copy that a user of that version executed. The update leg runs the copy from HEAD.

## What one leg does

Each leg with the script drivers has these phases:

1. Stage: make the bare clone, park `main` at the old release.
2. Install: run the old release's own installer script. Make sure that the checkout is at the old commit and that `hermes --version` works.
3. Desktop smoke: run `hermes desktop --build-only` from the installed CLI. This proves that the installed version can build the desktop app. If the installed version does not have this flag, the phase reports a skip and continues.
4. Update: move `main` to HEAD. Apply one update method. Make sure that the checkout is at HEAD and that `hermes --version` works.
5. Desktop smoke again, at HEAD.

The windows GUI driver replaces phases 2 and 4 when the install method is `desktop-installer@latest`. It downloads the published `Hermes-Setup.exe`, clicks through the installer window with AutoHotkey, and clicks "Update now" in the running app with Playwright.

## Old versions

A leg can install a release from months back. The driver must not assume that the old version has today's CLI surface. The rule: probe, do not assume.

- For the installer, read the flag from the old ref's own script text.
- For the installed CLI, ask the binary with `--help`.
- If a flag is not found, omit the flag. This is not an error.

## The install methods

- `installer-script`: the platform's one-liner (`curl | bash` on linux and macos, `irm | iex` on windows).
- `installer-script+desktop`: the same one-liner with its desktop stage opted in (`--include-desktop` / `-IncludeDesktop`). The stage builds the desktop app during the install. On windows it also registers Start Menu and Desktop shortcuts. On linux and macos it builds the app inside the checkout and registers no OS entry point.
- `desktop-installer@latest`: the published GUI installer (`Hermes-Setup.exe` on windows, `Hermes-Setup.dmg` on macos), driven through the real user flow.

## The two app-update variants

The desktop app has two launch paths, so the matrix has two app-update methods. Both click "Update now" in the running app. They differ in how the app starts:

- `open-app-update`: the app starts from the installed app entry point. On Windows, both the desktop installer and `installer-script+desktop` create shortcuts, so both support this route. On Linux and macOS, the script's opt-in desktop stage builds inside the checkout without registering an OS entry point. The macOS route therefore requires a desktop-installer install; Linux has no open-app-update leg.
- `hermes-desktop-app-update`: the app starts with the `hermes desktop` command. Every install method provides this command, on each OS that ships the desktop app. On linux this is the only app surface: no desktop installer and no packaged desktop artifact exist for linux. The driver captures the product's own launch call (argv, cwd, environment) with `e2e-assets/launch-capture/sitecustomize.py` and re-executes it under Playwright, which owns the app and clicks the update flow.

## Skips

A grey leg is normal. There are two causes:

- The method pair is declared but cannot run: either no OS entry point exists for it (open-app-update after a plain script install registers nothing to open), or no driver arm exists yet. Check the selected driver for supported pairs.
- The starting release predates the surface under test. Example: a release without `apps/desktop` has no window to launch. Inspect the selected release tree before choosing a desktop leg.

Record each selected leg as passed, failed or unsupported, including its start
release and target commit. [Confirmed historical upgrade limitations](KNOWN_FAILURES.md)
records failures in older releases. Those records are not blanket skips and do
not waive unrelated failures.

## Running and retaining evidence

Run only in disposable VMs. The Windows driver kills processes named Hermes and
the macOS driver operates on `/Applications/Hermes.app`. They can interfere with
an installed personal runtime.

For example, inside a disposable POSIX VM with a clean full-history checkout and
release tags:

```sh
tests/install/installer-script-e2e.sh --install-ref <release-tag> \
  --install-method installer-script --update-method hermes-update
```

Retain driver logs, exact refs, result files and GUI screenshots with the release
qualification evidence. GUI qualification also requires a screen recording with
nonzero frames. Supply native recording and display prerequisites in the VM,
including Xvfb for headless Linux GUI execution. The removed Actions artifact
uploader and recording action are no longer available. A portable source-gate
result alone does not establish successful installation, update or GUI behavior.
