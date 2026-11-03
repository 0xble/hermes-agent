#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * The checkout's test environment (via scripts/run-in-hermes-env when needed)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp_adapter/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).
#
# A missing or stale checkout environment is rebuilt by run-in-hermes-env,
# which runs setup-hermes.sh. That setup writes tool, dependency, and
# test-env state into the inherited HERMES_HOME, and PM writes the resolved
# root's tools/ (hermes_constants._hermes_root_of, pm/environments.store_root).
# The fallback refuses when that home, or the root it resolves to, is live:
# a current release pointer (including a dangling one), gateway_state.json,
# a gateway pid kill -0 can see (including EPERM from another user's gateway),
# or any path under the OS account default. A home under the account default
# resolves to that default; otherwise <root>/profiles/<name> resolves to <root>.
# The account default comes from the password database (getent, dscl, or
# ~user), never $HOME and never /usr/bin/python3. Workers pin HOME to scratch.
# If that lookup fails, the home is treated as live. Rebuild the checkout-local
# venv instead, from the checkout, after stopping anything that is using .venv
# and removing it if it exists:
#   python -m pm.build_env --source . --out .venv --group dev --group test
# Or set HERMES_HOME to a scratch directory that is not a live runtime home.
# HERMES_ALLOW_LIVE_HOME_SETUP=1 overrides the refusal only when setup against
# that home is deliberate. hermes update and the installer do not use this guard.

set -euo pipefail

# Predicate for the test-runner fallback. Sourcing this file defines it and
# returns; it does not run the suite. The runner body below executes only when
# this file is the program.
hermes_account_passwd_home() {
  local user home
  home=""
  user="$(id -un 2>/dev/null || true)"
  case "$user" in
    ""|*[!A-Za-z0-9._-]*) return 1 ;;
  esac
  if command -v getent >/dev/null 2>&1; then
    home="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
  fi
  if [ -z "$home" ] && command -v dscl >/dev/null 2>&1; then
    home="$(dscl . -read "/Users/${user}" NFSHomeDirectory 2>/dev/null \
      | sed -n 's/^NFSHomeDirectory: *//p' || true)"
  fi
  if [ -z "$home" ]; then
    # ~user reads the password database. ~ alone would follow $HOME.
    home="$(eval "echo ~${user}")" || return 1
  fi
  case "$home" in
    ""|"~${user}") return 1 ;;
  esac
  printf '%s\n' "$home"
}

hermes_account_default_home() {
  local account suffix os
  suffix="${HERMES_DATA_DIR_SUFFIX-}"
  os="$(uname -s 2>/dev/null || printf '%s' unknown)"
  case "$os" in
    MINGW*|MSYS*|CYGWIN*)
      if [ -n "${LOCALAPPDATA:-}" ]; then
        printf '%s\n' "${LOCALAPPDATA%/}/hermes${suffix}"
        return 0
      fi
      account="$(hermes_account_passwd_home)" || return 1
      printf '%s\n' "${account}/AppData/Local/hermes${suffix}"
      ;;
    *)
      account="$(hermes_account_passwd_home)" || return 1
      printf '%s\n' "${account}/.hermes${suffix}"
      ;;
  esac
}

hermes_canonical_home() {
  local path="${1%/}"
  [ -n "$path" ] || return 1
  if [ -d "$path" ]; then
    (cd "$path" && pwd -P) || return 1
    return 0
  fi
  printf '%s\n' "$path"
}

hermes_same_home() {
  local left right
  left="$(hermes_canonical_home "$1")" || return 1
  right="$(hermes_canonical_home "$2")" || return 1
  [ "$left" = "$right" ]
}

hermes_pid_from_file() {
  local file="$1" line pid
  [ -f "$file" ] || [ -L "$file" ] || return 1
  # JSON {"pid": N} or a legacy bare integer. Read only; never unlink.
  line="$(sed -n '1,20p' "$file" 2>/dev/null || true)"
  pid="$(printf '%s\n' "$line" | sed -n \
    -e 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
    -e 's/^\([0-9][0-9]*\)$/\1/p' | sed -n '1p')"
  [ -n "$pid" ] || return 1
  [ "$pid" -gt 0 ] 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

hermes_pid_is_alive() {
  local pid="$1" err
  # LC_ALL=C keeps the EPERM text stable. Discarding stderr would hide a
  # gateway owned by another user: kill -0 then exits 1 for both EPERM and ESRCH.
  if err="$(LC_ALL=C kill -0 "$pid" 2>&1)"; then
    return 0
  fi
  case "$err" in
    *"not permitted"*) return 0 ;;
  esac
  return 1
}

hermes_gateway_pid_running() {
  local home="$1" file pid
  for file in "$home/gateway.pid" "$home/gateway.b.pid"; do
    pid="$(hermes_pid_from_file "$file" 2>/dev/null || true)"
    [ -n "$pid" ] || continue
    if hermes_pid_is_alive "$pid"; then
      return 0
    fi
  done
  return 1
}

hermes_effective_test_home() {
  local os
  if [ -n "${HERMES_HOME:-}" ]; then
    printf '%s\n' "$HERMES_HOME"
    return 0
  fi
  os="$(uname -s 2>/dev/null || printf '%s' unknown)"
  case "$os" in
    MINGW*|MSYS*|CYGWIN*) printf '%s\n' "${LOCALAPPDATA:-${HOME:-}/AppData/Local}/hermes" ;;
    *) printf '%s\n' "${HOME:-}/.hermes" ;;
  esac
}

hermes_home_marker_reason() {
  local home="$1"
  [ -n "$home" ] || return 1
  if [ -L "$home/current" ]; then
    printf '%s\n' "current release pointer"
    return 0
  fi
  if [ -e "$home/gateway_state.json" ] || [ -L "$home/gateway_state.json" ]; then
    printf '%s\n' "gateway_state.json"
    return 0
  fi
  if hermes_gateway_pid_running "$home"; then
    printf '%s\n' "running gateway pid"
    return 0
  fi
  return 1
}

# True when home is root or a descendant. A sibling such as root-extra is not.
hermes_home_is_under() {
  local home="$1" root="$2" canon_home canon_root
  [ -n "$home" ] && [ -n "$root" ] || return 1
  canon_home="$(hermes_canonical_home "$home")" || return 1
  canon_root="$(hermes_canonical_home "$root")" || return 1
  [ "$canon_home" = "$canon_root" ] && return 0
  case "$canon_home" in
    "$canon_root"/*) return 0 ;;
  esac
  return 1
}

# Same rule as hermes_constants._hermes_root_of.
hermes_resolved_runtime_root() {
  local home="$1" native="${2-}" canon_home parent
  canon_home="$(hermes_canonical_home "$home")" || canon_home="${home%/}"
  if [ -n "$native" ] && hermes_home_is_under "$canon_home" "$native"; then
    hermes_canonical_home "$native"
    return 0
  fi
  parent="$(dirname "$canon_home")" || return 1
  case "$parent" in
    */profiles|profiles)
      dirname "$parent"
      return 0
      ;;
  esac
  printf '%s\n' "$canon_home"
}

# Prints why HOME is a live runtime home, or nothing and status 1.
# The optional second argument is an already-resolved account default.
hermes_runtime_home_live_reason() {
  local home="$1"
  local account_default="${2-}"
  local root reason
  [ -n "$home" ] || return 1
  reason="$(hermes_home_marker_reason "$home" || true)"
  if [ -n "$reason" ]; then
    printf '%s\n' "$reason"
    return 0
  fi
  if [ -z "$account_default" ]; then
    if ! account_default="$(hermes_account_default_home 2>/dev/null)"; then
      printf '%s\n' "account default lookup failed"
      return 0
    fi
  fi
  if hermes_home_is_under "$home" "$account_default"; then
    printf '%s\n' "under account default home"
    return 0
  fi
  root="$(hermes_resolved_runtime_root "$home" "$account_default")" || {
    printf '%s\n' "runtime root resolution failed"
    return 0
  }
  if ! hermes_same_home "$root" "$home"; then
    reason="$(hermes_home_marker_reason "$root" || true)"
    if [ -n "$reason" ]; then
      printf '%s\n' "$reason"
      return 0
    fi
  fi
  return 1
}

hermes_runtime_home_is_live() {
  hermes_runtime_home_live_reason "$@" >/dev/null
}

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
fi

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Even targeted runs compile the whole checkout. Acquire host admission before
# activation or compilation, not through spelling-dependent shell wrappers.
# The re-exec marker also handles heavy-slot's disabled / ancestor-reuse paths,
# which can exec the payload without setting HEAVY_SLOT_HELD.
if [ -z "${HEAVY_SLOT_HELD:-}${__HERMES_TESTS_HEAVY_SLOT:-}" ]; then
  # Match the terminal resolver when a service PATH omits ~/.local/bin.
  HEAVY_SLOT_BIN="$(command -v heavy-slot || true)"
  if [ -z "$HEAVY_SLOT_BIN" ] && [ -f "$HOME/.local/bin/heavy-slot" ] && [ -x "$HOME/.local/bin/heavy-slot" ]; then
    HEAVY_SLOT_BIN="$HOME/.local/bin/heavy-slot"
  fi
  if [ -n "$HEAVY_SLOT_BIN" ]; then
    exec "$HEAVY_SLOT_BIN" --label "scripts/run_tests.sh (in $REPO_ROOT)" -- \
      env __HERMES_TESTS_HEAVY_SLOT=1 "$BASH" "$SCRIPT_DIR/run_tests.sh" "$@"
  fi
fi

# ── Locate python ───────────────────────────────────────────────────────────
# The suite runs under the checkout's isolated test environment (pm.testenv),
# and only there: unless the inherited environment is current for this
# checkout, this script re-executes itself under scripts/run-in-hermes-env,
# which syncs and applies it. That holds however the script is launched
# (`scripts/run_tests.sh`, `bash scripts/run_tests.sh`, from CI or a shell), so
# a branch switch or lock edit never runs the suite against the previous
# dependency set.
#
# Without an activation, an explicit HERMES_PYTHON that has pytest is honored:
# the Nix devShell's editable venv and CI's minimal installer lanes provide
# one on purpose. The import check matters: a wrapped `hermes` binary exports
# HERMES_PYTHON pointing at a release venv without pytest.
_has_pytest() { [ -n "$1" ] && [ -x "$1" ] && "$1" -c 'import pytest' 2>/dev/null; }
# shellcheck source=scripts/_activation.sh
. "$SCRIPT_DIR/_activation.sh"
if [ -z "${__HERMES_ACTIVATED:-}" ] && _has_pytest "${HERMES_PYTHON:-}"; then
  PYTHON="$HERMES_PYTHON"
  # The Nix devShell's HERMES_PYTHON is the lock's interpreter, which finds the
  # uv2nix venv only through PYTHONPATH; keep it across `env -i` below.
  INHERITED_PYTHONPATH="${PYTHONPATH:-}"
  echo "▶ not activated — using HERMES_PYTHON: $PYTHON"
else
  INHERITED_PYTHONPATH=""
  if ! hermes_activation_current "$REPO_ROOT"; then
    if [ -n "${__HERMES_TESTS_REEXEC:-}" ]; then
      echo "error: run-in-hermes-env did not produce a current environment for $REPO_ROOT" >&2
      exit 1
    fi
    # Only this fallback. hermes update and the installer call setup directly.
    if [ "${HERMES_ALLOW_LIVE_HOME_SETUP:-}" != 1 ]; then
      _live_home="$(hermes_effective_test_home)"
      _live_reason="$(hermes_runtime_home_live_reason "$_live_home" || true)"
      if [ -n "$_live_reason" ]; then
        echo "error: refusing to run test-environment setup against a live runtime home: $_live_home ($_live_reason)." >&2
        echo "That setup writes tool, dependency, and test-env state into HERMES_HOME." >&2
        echo "Rebuild the checkout-local venv instead. From the checkout, stop anything using .venv, remove it if it exists, then run:" >&2
        echo "  python -m pm.build_env --source . --out .venv --group dev --group test" >&2
        echo "Or set HERMES_HOME to a scratch directory that is not a live runtime home." >&2
        echo "Set HERMES_ALLOW_LIVE_HOME_SETUP=1 only when setup against this home is deliberate." >&2
        exit 1
      fi
    fi
    echo "▶ environment missing or stale for $REPO_ROOT — re-running under run-in-hermes-env" >&2
    export __HERMES_TESTS_REEXEC=1
    exec "$SCRIPT_DIR/run-in-hermes-env" "$BASH" "${BASH_SOURCE[0]}" "$@"
  fi
  PYTHON="${__HERMES_TEST_PYTHON:-}"
  if ! _has_pytest "$PYTHON"; then
    echo "error: activation provided no test interpreter with pytest (__HERMES_TEST_PYTHON=${PYTHON:-unset})" >&2
    exit 1
  fi
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH="$INHERITED_PYTHONPATH"
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="${EXTRA_PYTHONPATH:+$EXTRA_PYTHONPATH:}$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Windows location variables (computed before we drop env) ───────────────
# `env -i` forwards HOME, which is enough on POSIX. Native Windows CPython
# resolves Path.home() from USERPROFILE (or HOMEDRIVE+HOMEPATH), stdlib
# platform paths come from LOCALAPPDATA/APPDATA, ssl/sockets need SYSTEMROOT,
# and tempfile needs TEMP/TMP. Dropping them breaks collection on native
# Windows (issues #67385, #70813). PATHEXT is also required: without .EXE,
# PowerShell opens a native child as a document without waiting for its exit.
# These are location variables, not
# credentials, so forwarding them keeps the isolation intent intact. Each is
# only forwarded when actually set, so POSIX runs are byte-for-byte unchanged.
# Portable CI also binds PowerShell's module-analysis cache to its disposable
# parent home; retain that exact location across the hermetic env boundary.
WIN_ENV=()
for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP \
    ComSpec PATHEXT PROGRAMFILES ProgramFiles PROGRAMDATA ProgramData PSModuleAnalysisCachePath; do
  if [ -n "${!_win_var:-}" ]; then
    WIN_ENV+=("$_win_var=${!_win_var}")
  fi
done
# Missing and explicitly empty differ for PowerShell command discovery. Keep
# an explicit value (including empty); on Windows use empty when absent so a
# hermetic child does not rebuild the hosted machine's module search path.
# Built-in PowerShell modules remain available. This is a location, not a secret.
if [ "${PSModulePath+x}" = x ] || [ -n "${SYSTEMROOT:-}" ]; then
  WIN_ENV+=("PSModulePath=${PSModulePath-}")
fi
# Native build toolchain (Windows arm64 has no wheels for every pinned C extension, so
# `uv sync` inside a PM test compiles ruamel-yaml-clib and friends). The MSVC developer
# environment is exported by scripts/build/windows-deps.ps1 into the job env; without
# INCLUDE/LIB/VSINSTALLDIR the build backend reports "Visual C++ 14.0 or greater is
# required". These describe compiler locations, not credentials.
for _tool_var in INCLUDE LIB LIBPATH VSINSTALLDIR VCINSTALLDIR VCToolsInstallDir VCToolsVersion \
    VCToolsRedistDir WindowsSdkDir WindowsSDKVersion WindowsSdkBinPath WindowsSdkVerBinPath \
    WindowsLibPath UCRTVersion UniversalCRTSdkDir VSCMD_ARG_HOST_ARCH VSCMD_ARG_TGT_ARCH VSCMD_VER \
    DevEnvDir ExtensionSdkDir Platform CARGO_HOME RUSTUP_HOME RUSTUP_TOOLCHAIN \
    CARGO_TARGET_AARCH64_PC_WINDOWS_MSVC_LINKER CC_aarch64_pc_windows_msvc CC CXX AR \
    VCPKG_ROOT OPENSSL_DIR OPENSSL_STATIC OPENSSL_LIB_DIR OPENSSL_INCLUDE_DIR; do
  if [ -n "${!_tool_var:-}" ]; then
    WIN_ENV+=("$_tool_var=${!_tool_var}")
  fi
done
# setuptools locates the compiler through vswhere under "%ProgramFiles(x86)%\Microsoft Visual
# Studio\Installer"; without that variable a primed INCLUDE/LIB still reads as "Visual C++ 14.0
# or greater is required". The parenthesised name cannot be read with ${!var}.
_pf86="$(env | sed -n 's/^ProgramFiles(x86)=//p' | head -n1)"
[ -z "$_pf86" ] || WIN_ENV+=("ProgramFiles(x86)=$_pf86")

# ── Test-runner knobs (computed before we drop env) ────────────────────────
# The runner's own documented environment knobs must survive the hermetic
# `env -i` below, or they are silent no-ops for anyone invoking this script:
#
#   * HERMES_TEST_WORKERS / PATHS / FILE_TIMEOUT / FILE_RETRIES / SLICE are
#     read by run_tests_parallel.py at argparse-default time — inside the
#     stripped environment.
#   * HERMES_TEST_SCRATCH_ROOT selects an explicit disk-backed fixture root
#     when the sandbox makes the default /var/tmp read-only.
#   * HERMES_TEST_IMAGE is read by tests/docker/conftest.py to skip its
#     session-scoped `docker build`. CI's docker.yml sets it to the image
#     the build step just loaded; stripping it made every per-file pytest
#     subprocess rebuild the 5GB image from a cold builder cache instead
#     (~4 min per worker per run, and the rebuilt image lacked the
#     HERMES_GIT_SHA build-arg the workflow bakes in).
#   * HERMES_E2E_REQUIRE_TUI turns a missing Ink TUI build into a failure in
#     tests/e2e/core/terminal instead of a skip (set by the e2e CI job).
#   * CI / GITHUB_ACTIONS tell suites they run on a disposable runner (e.g.
#     tests/e2e/core/upgrade runs the real updater unsandboxed only there).
#   * HERMES_E2E_WINDOWS_INSTALL opts tests/e2e/core/windows_update into running
#     the real install.ps1 (it writes HKCU PATH); HERMES_E2E_MACHINE_ROOT,
#     HERMES_E2E_PROFILES_ROOT and HERMES_E2E_ARTIFACTS place its fake machines,
#     their user profiles and the transcripts CI uploads.
#   * HERMES_E2E_STRICT_ACCEPTANCE makes tests/e2e/core/_pending_fixes.py's
#     known gaps hard failures in an acceptance run (workflow dispatch input).
#   * HERMES_TEST_SSH_HOST / _USER / _PORT / _KEY and HERMES_TEST_DOCKER_IMAGE
#     arm the real-backend tests (tests/tools/test_ssh_environment.py,
#     tests/tools/test_docker_backend_real.py); remote-backends.yml points them at
#     a throwaway sshd container and a small public image. The key is a
#     per-run throwaway, never a credential.
#
# These are test-infrastructure knobs, not credentials — same class as the
# HERMES_RUN_SLOW_PET_TESTS / HERMES_E2E_BROWSER / HERMES_RUN_E2E opt-ins
# forwarded below.
# SSL_CERT_FILE/DIR are trust-store locations: the pinned interpreter's
# OpenSSL has no compiled-in bundle path on NixOS, so network tests (PM
# downloads, channel reads) need the host's pointer to verify TLS.
# Keep this an explicit allowlist (no HERMES_TEST_* glob) so the "no
# credential can leak" property stays auditable at a glance.
# Admission state is deliberately scrubbed: the helper recognizes held ancestors
# after env -i, while tests must still exercise terminal promotion as unheld.
TEST_ENV=()
for _test_var in HERMES_PYTHON HERMES_TEST_IMAGE HERMES_TEST_PATHS \
  HERMES_TEST_FILE_TIMEOUT HERMES_TEST_FILE_RETRIES HERMES_TEST_SLICE \
  HERMES_TEST_SCRATCH_ROOT PLAYWRIGHT_BROWSERS_PATH SSL_CERT_FILE SSL_CERT_DIR HERMES_GATEWAY_LOCK_DIR HERMES_E2E_REQUIRE_TUI CI GITHUB_ACTIONS \
  HERMES_E2E_WINDOWS_INSTALL HERMES_E2E_MACHINE_ROOT HERMES_E2E_PROFILES_ROOT HERMES_E2E_ARTIFACTS \
  HERMES_E2E_STRICT_ACCEPTANCE HERMES_TEST_SSH_HOST HERMES_TEST_SSH_USER HERMES_TEST_SSH_PORT \
  HERMES_TEST_SSH_KEY HERMES_TEST_DOCKER_IMAGE; do
  if [ -n "${!_test_var:-}" ]; then
    TEST_ENV+=("$_test_var=${!_test_var}")
  fi
done

# Portable CI supplies a fresh global config containing only the current
# checkout's safe.directory. Keep that exact path across env -i so Git works
# when the Actions container runs as root over a runner-owned workspace. Do
# not forward repository selectors, credential helpers, or arbitrary GIT_* vars.
GIT_ENV=()
for _git_var in GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM; do
  if [ -n "${!_git_var:-}" ]; then
    GIT_ENV+=("$_git_var=${!_git_var}")
  fi
done

# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
# Whole-checkout compilation also happens on targeted runs. Keep it within the
# same per-job budget as the file runner, including shells without host dotfiles.
export HERMES_TEST_WORKERS="${HERMES_TEST_WORKERS:-4}"
if [[ ! "$HERMES_TEST_WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: HERMES_TEST_WORKERS must be a positive integer" >&2
  exit 1
fi
TEST_ENV+=("HERMES_TEST_WORKERS=$HERMES_TEST_WORKERS")
echo "▶ pre-compiling bytecode cache (up to $HERMES_TEST_WORKERS workers)"
"$PYTHON" -m compileall -q -j "$HERMES_TEST_WORKERS" -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  ${TEST_ENV[@]+"${TEST_ENV[@]}"} \
  ${GIT_ENV[@]+"${GIT_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_E2E_BROWSER:+HERMES_E2E_BROWSER="$HERMES_E2E_BROWSER"} \
  ${HERMES_RUN_E2E:+HERMES_RUN_E2E="$HERMES_RUN_E2E"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
