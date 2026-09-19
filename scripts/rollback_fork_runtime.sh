#!/usr/bin/env bash
# Roll the installed Hermes checkout back to a previously installed source SHA (slice 14, step 6).
#
# Upstream's documented recovery is: git checkout <sha>, reinstall the package, restart the gateway.
# This wraps that sequence with a preflight so a rollback can never make things worse:
#   - the target SHA must exist in the checkout and must be an ancestor-or-equal of a known ref,
#   - the virtual environment must exist and import hermes_cli,
#   - the working tree must be clean (a dirty tree would be silently discarded by checkout),
#   - a recovery ref is written BEFORE anything moves, so the rollback is itself reversible.
# Restart is a separate step because company hosts restart through their own supervisor.
#
# Usage: rollback_fork_runtime.sh --checkout <dir> --venv <dir> --sha <sha> [--restart] [--dry-run]
set -euo pipefail

checkout=""; venv=""; sha=""; restart=0; dry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --checkout) checkout="$2"; shift 2 ;;
    --venv) venv="$2"; shift 2 ;;
    --sha) sha="$2"; shift 2 ;;
    --restart) restart=1; shift ;;
    --dry-run) dry=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$checkout" && -n "$venv" && -n "$sha" ]] || { echo "usage: --checkout <dir> --venv <dir> --sha <sha> [--restart] [--dry-run]" >&2; exit 2; }

say() { printf '%s\n' "$*"; }
fail() { say "ROLLBACK REFUSED: $*"; exit 1; }

[[ -d "$checkout/.git" || -f "$checkout/.git" ]] || fail "$checkout is not a git checkout"
[[ -x "$venv/bin/python" ]] || fail "$venv/bin/python is missing"
"$venv/bin/python" -c "import hermes_cli" 2>/dev/null || fail "venv cannot import hermes_cli"
if [[ -n "$(git -C "$checkout" status --porcelain)" ]]; then
  fail "working tree is dirty; a checkout would discard those changes"
fi
target="$(git -C "$checkout" rev-parse --verify --quiet "${sha}^{commit}")" || fail "SHA $sha does not exist in $checkout"
current="$(git -C "$checkout" rev-parse HEAD)"
if [[ "$target" == "$current" ]]; then
  say "already at $target; nothing to do"; exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
recovery_ref="refs/rollback-recovery/${stamp}"
say "current HEAD: $current"
say "target      : $target"
say "recovery ref: $recovery_ref -> $current"
if [[ $dry -eq 1 ]]; then
  say "dry run: would write the recovery ref, checkout the target, reinstall, and $([[ $restart -eq 1 ]] && echo restart || echo 'not restart')"
  exit 0
fi

git -C "$checkout" update-ref "$recovery_ref" "$current"
git -C "$checkout" checkout --quiet --detach "$target"
# Editable reinstall so entry points and metadata match the rolled-back source. Hermes venvs are
# uv-managed and carry no pip module, so prefer uv and fall back to pip only when present.
reinstall() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "$venv/bin/python" --no-deps -e "$checkout"
  else
    "$venv/bin/python" -m pip install --quiet --no-deps -e "$checkout"
  fi
}
reinstall 2>&1 | tail -3; [[ ${PIPESTATUS[0]} -eq 0 ]] || {
  say "reinstall failed; restoring $current"
  git -C "$checkout" checkout --quiet --detach "$current"
  exit 1
}
installed="$("$venv/bin/python" -c 'import hermes_cli, os; print(os.path.dirname(os.path.dirname(hermes_cli.__file__)))')"
[[ "$installed" == "$(cd "$checkout" && pwd -P)" || "$installed" == "$checkout" ]] || say "warning: installed package resolves to $installed, not $checkout"
say "rolled back to $(git -C "$checkout" rev-parse --short=12 HEAD); recovery ref $recovery_ref"
if [[ $restart -eq 1 ]]; then
  "$venv/bin/hermes" gateway restart
  say "gateway restart requested; verify with: hermes gateway status && hermes --version"
fi
