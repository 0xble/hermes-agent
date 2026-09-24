#!/usr/bin/env bash
# Personal-host scheduling adapter. Code and tests remain owned by the fork.
#
# The scheduled sync owns .worktrees/fork-sync exclusively; interactive work
# uses its own worktrees. Local CI evidence is the repository contract's
# exact-SHA `bin/ci gate`; the merge authority is the PR's GitHub
# `qualification` check. --dry-run builds and gates the candidate without
# publishing anything.
set -euo pipefail
profile_home="${HERMES_HOME:-$HOME/.hermes}"
source_repo="$HOME/Repos/hermes-agent"
worktree="$source_repo/.worktrees/fork-sync"
export HERMES_PYTHON="$source_repo/.venv/bin/python"
export PATH="$source_repo/.venv/bin:$PATH"
runtime_python="$profile_home/hermes-agent/venv/bin/python"
"$HERMES_PYTHON" -c 'import pytest, hindsight_client_api'
publish=(--publish)
gate_only=0
passthrough=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) publish=() ;;
    # Gate exact origin/main without merging the newest upstream tag, for
    # pinned target policies and verification runs. Never publishes.
    --gate-only) gate_only=1; publish=() ;;
    *) passthrough+=("$arg") ;;
  esac
done
receipt_dir="$profile_home/maintenance/fork-sync"
mkdir -p "$receipt_dir"
receipt="$receipt_dir/$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
if [ "$gate_only" -eq 1 ]; then
  if [ -z "${HERMES_FORK_SYNC_LOCKED:-}" ]; then
    # Same fcntl lock the sync script takes; the descriptor survives exec.
    lock="$(git -C "$source_repo" rev-parse --path-format=absolute --git-common-dir)/hermes-fork-sync.lock"
    exec "$runtime_python" -c 'import fcntl, os, sys
f = open(sys.argv[1], "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("[CRON_FAILURE] fork sync: another fork sync is running"); sys.exit(1)
os.set_inheritable(f.fileno(), True)
os.environ["HERMES_FORK_SYNC_LOCKED"] = "1"
os.execvp("bash", ["bash", *sys.argv[2:]])' "$lock" "$0" "$@"
  fi
  git -C "$source_repo" fetch --quiet --no-tags origin refs/heads/main:refs/remotes/origin/main
  if [ ! -e "$worktree" ]; then
    git -C "$source_repo" worktree add --quiet --detach "$worktree" origin/main
    git -C "$source_repo" worktree lock --reason "scheduled Hermes fork sync" "$worktree"
  fi
  if [ -n "$(git -C "$worktree" status --porcelain)" ]; then
    echo "[CRON_FAILURE] fork sync: $worktree is dirty; refusing to gate"; exit 1
  fi
  "$runtime_python" - "$receipt" "$(git -C "$source_repo" rev-parse origin/main)" <<'PY'
import json, sys
from datetime import datetime, timezone
json.dump({"mode": "gate_only", "status": "gated", "candidate": sys.argv[2],
           "started_at": datetime.now(timezone.utc).isoformat()},
          open(sys.argv[1], "w", encoding="utf-8"), indent=2, sort_keys=True)
PY
else
  set +e
  "$runtime_python" "$profile_home/scripts/sync_fork_candidate.py" \
    --repo "$worktree" --source-repo "$source_repo" \
    --candidate origin/main --upstream-remote upstream-live --origin-remote origin \
    --verify-current ${publish[@]+"${publish[@]}"} --result "$receipt" \
    ${passthrough[@]+"${passthrough[@]}"}
  build=$?
  set -e
  [ "$build" -eq 0 ] || exit "$build"
fi

read -r sha work < <("$runtime_python" -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r.get("candidate_head") or r["candidate"], r.get("branch", ""))' "$receipt")
git -C "$worktree" checkout --quiet --detach "$sha"
if [ ${#publish[@]} -eq 0 ] && [ -n "$work" ]; then
  git -C "$worktree" branch -D "$work" >/dev/null
fi
tree=$(git -C "$worktree" rev-parse "$sha^{tree}")
log="$receipt_dir/$(basename "$receipt" .json)-gate.log"
set +e
(cd "$worktree" && ./bin/ci gate "$sha") >"$log" 2>&1
gate=$?
set -e
"$runtime_python" - "$receipt" "$sha" "$tree" "$gate" "$log" <<'PY'
import json, sys
path, sha, tree, code, log = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
result = json.load(open(path, encoding="utf-8"))
result["local_ci"] = {
    "command": f"./bin/ci gate {sha}", "head_sha": sha, "tree_sha": tree,
    "exit_code": code, "status": "completed",
    "conclusion": "success" if code == 0 else "failure", "log": log,
    "merge_authority": {"check": "qualification", "app_id": 15368, "strict": True},
}
if code:
    result["status"] = "gate_failed"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
if [ "$gate" -ne 0 ]; then
  echo "[CRON_FAILURE] fork sync: exact-SHA gate failed for $sha (receipt $receipt, log $log)"
  exit 1
fi
echo "fork sync: exact-SHA gate passed for $sha (receipt $receipt)"
