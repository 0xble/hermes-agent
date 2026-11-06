# Sourced, read-only setup admission. Resolution belongs to the Python runtime.
# Source-only use changes no shell options and performs no writes.

hermes_account_default_home() {
  local repo python
  repo="$(hermes_guard_default_repo)" || return 1
  python="$(hermes_guard_python "$repo")" || return 1
  hermes_guard_query "$repo" "$python" account
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


# REPO and PYTHON are explicit on the setup path. The default only preserves
# source-only predicate callers; no PATH python (notably the macOS stub) is run.
hermes_guard_query() (
  local repo="$1" python="$2"
  shift 2
  cd "$repo" || return 1
  unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
  export UV_NO_CONFIG=1
  "$python" -B "$repo/scripts/setup_guard.py" "$@"
)

hermes_guard_python() {
  local repo="$1" candidate
  for candidate in "$repo/.venv/bin/python" "$repo/.venv/Scripts/python.exe" \
                   "$repo/venv/bin/python" "$repo/venv/Scripts/python.exe"; do
    [ -x "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
  done
  return 1
}

hermes_guard_default_repo() {
  local source="${BASH_SOURCE[0]}"
  cd "${source%/*}/.." && pwd -P
}

hermes_resolve_path() {
  local repo python
  repo="$(hermes_guard_default_repo)" || return 1
  python="$(hermes_guard_python "$repo")" || return 1
  # Preserve the caller's cwd for this standalone compatibility predicate.
  (unset PYTHONHOME PYTHONPATH VIRTUAL_ENV; "$python" -B "$repo/scripts/setup_guard.py" resolve "$1")
}

# Check every lexical AND resolved record, even when two paths are equal.
# Python classifies account-default containment; shell only checks live markers.
hermes_guard_records_live_reason() {
  local records="$1" kind candidate reason
  while IFS=$'\t' read -r kind candidate; do
    case "$kind" in
      account) printf '%s\n' "under account default home"; return 0 ;;
      path)
        [ -n "$candidate" ] || { printf '%s\n' "runtime root resolution failed"; return 0; }
        reason="$(hermes_home_marker_reason "$candidate" || true)"
        [ -z "$reason" ] || { printf '%s\n' "$reason"; return 0; }
        ;;
      *) printf '%s\n' "runtime root resolution failed"; return 0 ;;
    esac
  done <<< "$records"
  return 1
}

hermes_runtime_home_live_reason() {
  local repo python records
  repo="$(hermes_guard_default_repo)" || return 0
  python="$(hermes_guard_python "$repo")" || {
    printf '%s\n' "runtime root resolution failed (no interpreter)"; return 0;
  }
  records="$(hermes_guard_query "$repo" "$python" paths "$@")" || {
    printf '%s\n' "runtime root resolution failed"; return 0;
  }
  hermes_guard_records_live_reason "$records"
}

hermes_runtime_home_is_live() {
  hermes_runtime_home_live_reason "$@" >/dev/null
}

# The single setup admission check, shared by the test runner and activation.
# No shell resolver fallback: unavailable runtime resolution always refuses.
hermes_require_safe_setup() {
  local repo="$1" python="${2-}" records reason live=0
  [ "${HERMES_ALLOW_LIVE_HOME_SETUP:-}" = 1 ] && return 0
  if [ -z "$python" ]; then
    python="$(cd "$repo" && hermes_bootstrap_python "$repo")" || python=""
  fi
  if [ -z "$python" ] || ! records="$(hermes_guard_query "$repo" "$python" paths)"; then
    reason="runtime root resolution failed (interpreter unavailable or runtime import failed)"
  else
    reason="$(hermes_guard_records_live_reason "$records")" || live=$?
    [ "$live" -eq 1 ] && return 0
    [ -n "$reason" ] || reason="runtime root resolution failed"
  fi
  echo "error: refusing to run test-environment setup against a live runtime home: ${HERMES_HOME:-<default>} ($reason)." >&2
  echo "That setup writes tool, dependency, and test-env state into HERMES_HOME." >&2
  echo "Rebuild the checkout-local venv instead. From the checkout, stop anything using .venv, remove it if it exists, then run:" >&2
  echo "  python -m pm.build_env --source . --out .venv --group dev --group test" >&2
  echo "Or set HERMES_HOME to a scratch directory that is not a live runtime home." >&2
  echo "Set HERMES_ALLOW_LIVE_HOME_SETUP=1 only when setup against this home is deliberate." >&2
  return 1
}
