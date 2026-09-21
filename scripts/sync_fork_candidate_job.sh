#!/usr/bin/env bash
# Personal-host scheduling adapter. Code and tests remain owned by the fork.
set -euo pipefail
profile_home="${HERMES_HOME:-$HOME/.hermes}"
source_repo="$HOME/Repos/hermes-agent"
export HERMES_PYTHON="$source_repo/.venv/bin/python"
export PATH="$source_repo/.venv/bin:$PATH"
"$HERMES_PYTHON" -c 'import pytest, hindsight_client_api'
receipt_dir="$profile_home/maintenance/fork-sync"
mkdir -p "$receipt_dir"
receipt="$receipt_dir/$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
exec "$profile_home/hermes-agent/venv/bin/python" "$profile_home/scripts/sync_fork_candidate.py" \
  --repo "$source_repo/.worktrees/sync" --source-repo "$source_repo" \
  --candidate origin/main --upstream-remote upstream-live --origin-remote origin \
  --verify-current --publish --result "$receipt" "$@"
