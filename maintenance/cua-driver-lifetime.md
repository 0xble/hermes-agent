# Embedded cua-driver lifetime

Load when changing the private `cua-driver serve --embedded` spawn, its stop path, or gateway/CLI startup cleanup of `hc-` sockets.

## Preserve

Fork patch identity: `cua-driver-lifetime`.

An embedded driver must not outlive the Hermes process that spawned it. On macOS the driver is still launched through `/usr/bin/open` so TCC stays on `com.trycua.driver`. `open` exits immediately and does not pass stdin, so the driver's `--parent-liveness-stdio` / `CUA_DRIVER_PARENT_LIVENESS_STDIN` watch cannot see the host. Do not switch that launch to a direct spawn to get the flag: that drops the TCC identity the macOS spawn tests pin.

The owner is the spawning process. It writes an `hc-*.sock.owner` record, holds a pipe, and on session release or `stop()` kills the driver's own process group after `cua-driver stop --socket`. A watchdog in its own session reaps that driver when the pipe EOFs and the owner pid is dead, including SIGKILL and a skipped atexit. Closing the pipe while the owner is still alive must not kill the driver.

The gateway and CLI startup sweep reaps only ppid-1 processes of this user whose executable realpath is an exact known cua-driver binary and whose `--socket` basename starts with `hc-`, and only when the owner record is dead or absent and no live-parent process still names the socket. It never signals a driver whose owner pid is alive. It also unlinks stale `hc-` sockets, not regular files with that prefix.

## Provenance and patches

- **Identity and status:** `cua-driver-lifetime`, active.
- **Source / fork refs:** Maintained checkout `origin/main` `532b1885d6ee24fa18f258a4091f61eeb874008e`. Spawn is `tools/computer_use/cua_backend_daemon.py` (`_embedded_daemon_spawn_command`, `_EmbeddedCuaDaemon.start` / `stop`). Session release is `release_computer_use_session` and `agent/client_lifecycle.py`.
- **Surfaces:** `tools/computer_use/cua_driver_lifetime.py`, `tools/computer_use/cua_backend_daemon.py`, `hermes_cli/main.py`, `gateway/run_bootstrap.py`, `tests/computer_use/test_cua_driver_lifetime.py`.
- **Contribution route and rationale:** Local fork adaptation. The installed driver accepts `--parent-liveness-stdio`, but `open` cannot deliver the pipe. No orphan sweep or owner record existed in the maintained tree.
- **Upstream disposition:** Checked the maintained tree at the revision above (root computer-use daemon and CLI/gateway startup). No parent-death tie and no `hc-` sweep. `upstream-live` blob lookup timed out; do not treat that as an upstream fix.
- **Fork delivery:** Local commit on `fix/cua-driver-lifetime`. Not pushed.

## Update

On a later sync, if upstream launches the embedded daemon as a direct child and wires `--parent-liveness-stdio` without dropping the macOS TCC identity, adopt that and delete the watchdog. Keep the startup sweep until upstream reaps ppid-1 `hc-` drivers without killing a live owner.

## Verify and recover

`scripts/run_tests.sh tests/computer_use/test_cua_driver_lifetime.py tests/computer_use/test_cua_atexit_teardown.py tests/tools/test_computer_use_cua_macos_identity.py tests/tools/test_computer_use_cua_0_10_permissions.py tests/computer_use/test_cua_no_overlay.py`

Retire when the accepted upstream release ties the embedded driver to its owner across crash and `open` detachment, and these tests pass without the fork module.
