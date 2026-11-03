# Sourced, read-only setup guard. No activation, setup, or shell-option changes.
# Both run_tests.sh and hermes_sync use this one resolver and refusal check.

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

# Mirror of Python Path.resolve(strict=False). Walk to the deepest existing
# ancestor, canonicalize it with cd -P / pwd -P (a symlink leaf included), and
# re-append missing suffix components, normalizing . and .. the way Python does.
# A later .. that pops back into an existing path is resolved again, so a
# symlink after a missing component is not left lexical. Resolution errors
# return 1. This is the only canonicalizer in the guard.
hermes_resolve_path() {
  local input="${1-}"
  local abs rest path name candidate target seen depth steps
  [ -n "$input" ] || return 1
  case "$input" in
    *$'\n'*) return 1 ;;
  esac
  case "$input" in
    /*) abs="$input" ;;
    *)
      abs="$(pwd -P)" || return 1
      abs="${abs%/}/$input"
      ;;
  esac
  rest="${abs#/}"
  path="/"
  seen=$'\n'
  depth=0
  steps=0
  while [ -n "$rest" ]; do
    steps=$((steps + 1))
    [ "$steps" -le 4096 ] || return 1
    name="${rest%%/*}"
    case "$rest" in
      */*) rest="${rest#*/}" ;;
      *) rest="" ;;
    esac
    case "$name" in
      ""|.) continue ;;
    esac
    if [ "$name" = ".." ]; then
      if [ "$path" != "/" ]; then
        path="${path%/*}"
        [ -n "$path" ] || path="/"
      fi
      continue
    fi
    if [ "$path" = "/" ]; then
      candidate="/${name}"
    else
      candidate="${path}/${name}"
    fi
    if [ -d "$candidate" ]; then
      path="$(cd -P "$candidate" && pwd -P)" || return 1
      continue
    fi
    if [ -L "$candidate" ]; then
      case "$seen" in
        *$'\n'"${candidate}"$'\n'*) return 1 ;;
      esac
      seen="${seen}${candidate}"$'\n'
      depth=$((depth + 1))
      [ "$depth" -le 40 ] || return 1
      target="$(readlink "$candidate")" || return 1
      case "$target" in
        /*) path="/" ;;
      esac
      target="${target#/}"
      target="${target%/}"
      if [ -n "$target" ]; then
        if [ -n "$rest" ]; then
          rest="${target}/${rest}"
        else
          rest="$target"
        fi
      fi
      continue
    fi
    if [ -e "$candidate" ]; then
      path="$candidate"
      continue
    fi
    path="$candidate"
  done
  printf '%s\n' "$path"
}

hermes_same_home() {
  local left right
  left="$(hermes_resolve_path "$1")" || return 2
  right="$(hermes_resolve_path "$2")" || return 2
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
  [ -n "$home" ] && [ -n "$root" ] || return 2
  canon_home="$(hermes_resolve_path "$home")" || return 2
  canon_root="$(hermes_resolve_path "$root")" || return 2
  [ "$canon_home" = "$canon_root" ] && return 0
  case "$canon_home" in
    "$canon_root"/*) return 0 ;;
  esac
  return 1
}

# Same rule as hermes_constants._hermes_root_of: compare the resolved path with
# the native home, but keep the profiles parent lexical. Canonicalizing first
# follows a symlink at <root>/profiles/<name> and loses <root>.
hermes_resolved_runtime_root() {
  local home="$1" native="${2-}" lexical parent under
  lexical="${home%/}"
  [ -n "$lexical" ] || return 1
  if [ -n "$native" ]; then
    under=0
    hermes_home_is_under "$lexical" "$native" || under=$?
    if [ "$under" -eq 0 ]; then
      hermes_resolve_path "$native" || return 1
      return 0
    fi
    if [ "$under" -ne 1 ]; then
      return 1
    fi
  fi
  parent="$(dirname "$lexical")" || return 1
  case "${parent##*/}" in
    profiles)
      dirname "$parent"
      return 0
      ;;
  esac
  printf '%s\n' "$lexical"
}

# The same rule applied after Path.resolve(strict=False). A resolution error
# is a failure, not a lexical fallback.
hermes_canonical_runtime_root() {
  local home="$1" native="${2-}" canon
  canon="$(hermes_resolve_path "$home")" || return 1
  hermes_resolved_runtime_root "$canon" "$native"
}

# Prints why HOME is a live runtime home, or nothing and status 1.
# The optional second argument is an already-resolved account default.
hermes_runtime_home_live_reason() {
  local home="$1"
  local account_default="${2-}"
  local root canon_root candidate reason under same
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
  if [ -n "$account_default" ]; then
    under=0
    hermes_home_is_under "$home" "$account_default" || under=$?
    if [ "$under" -eq 0 ]; then
      printf '%s\n' "under account default home"
      return 0
    fi
    if [ "$under" -ne 1 ]; then
      printf '%s\n' "runtime root resolution failed"
      return 0
    fi
  fi
  root="$(hermes_resolved_runtime_root "$home" "$account_default")" || {
    printf '%s\n' "runtime root resolution failed"
    return 0
  }
  canon_root="$(hermes_canonical_runtime_root "$home" "$account_default")" || {
    printf '%s\n' "runtime root resolution failed"
    return 0
  }
  # PM writes the lexical root. If canonicalizing names a different root,
  # setup can still follow the symlink, so either live root refuses.
  for candidate in "$root" "$canon_root"; do
    same=0
    hermes_same_home "$candidate" "$home" || same=$?
    if [ "$same" -eq 0 ]; then
      continue
    fi
    if [ "$same" -ne 1 ]; then
      printf '%s\n' "runtime root resolution failed"
      return 0
    fi
    reason="$(hermes_home_marker_reason "$candidate" || true)"
    if [ -n "$reason" ]; then
      printf '%s\n' "$reason"
      return 0
    fi
    under=0
    hermes_home_is_under "$candidate" "$account_default" || under=$?
    if [ "$under" -eq 0 ]; then
      printf '%s\n' "under account default home"
      return 0
    fi
    if [ "$under" -ne 1 ]; then
      printf '%s\n' "runtime root resolution failed"
      return 0
    fi
  done
  return 1
}

hermes_runtime_home_is_live() {
  hermes_runtime_home_live_reason "$@" >/dev/null
}

# The single setup admission check, shared by the test runner and activation.
# Return nonzero before any setup write. Only the exact opt-in value 1 bypasses.
hermes_require_safe_setup() {
  local home reason live=0
  [ "${HERMES_ALLOW_LIVE_HOME_SETUP:-}" = 1 ] && return 0
  home="$(hermes_effective_test_home)" || return 1
  reason="$(hermes_runtime_home_live_reason "$home")" || live=$?
  [ "$live" -eq 1 ] && return 0
  if [ "$live" -ne 0 ] || [ -z "$reason" ]; then
    reason="runtime root resolution failed"
  fi
  echo "error: refusing to run test-environment setup against a live runtime home: $home ($reason)." >&2
  echo "That setup writes tool, dependency, and test-env state into HERMES_HOME." >&2
  echo "Rebuild the checkout-local venv instead. From the checkout, stop anything using .venv, remove it if it exists, then run:" >&2
  echo "  python -m pm.build_env --source . --out .venv --group dev --group test" >&2
  echo "Or set HERMES_HOME to a scratch directory that is not a live runtime home." >&2
  echo "Set HERMES_ALLOW_LIVE_HOME_SETUP=1 only when setup against this home is deliberate." >&2
  return 1
}
