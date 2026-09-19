#!/usr/bin/env bash
# Slice 18, step 1: archive the legacy fork's complete code history separately from its data.
#
# Produces a verified git bundle of EVERY ref in the legacy checkout (branches, tags, remote-
# tracking refs, stashes, and the unmerged upstream-PR branches such as #106906 and #106101),
# plus a manifest of ref -> SHA and a list of dirty or untracked paths that a bundle cannot
# carry. Verifies the bundle with `git bundle verify` and proves it restores by cloning it into
# a scratch directory and comparing ref counts. Never modifies the legacy checkout.
#
# Usage: archive_legacy_fork.sh --legacy <checkout> --out <dir>
set -euo pipefail
legacy=""; out=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --legacy) legacy="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$legacy" && -n "$out" ]] || { echo "usage: --legacy <checkout> --out <dir>" >&2; exit 2; }
[[ -d "$legacy/.git" || -f "$legacy/.git" ]] || { echo "$legacy is not a git checkout" >&2; exit 1; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dir="$out/legacy-fork-archive-$stamp"
mkdir -p "$dir"
head="$(git -C "$legacy" rev-parse HEAD)"
echo "legacy HEAD: $head"

# 1. Everything a bundle can carry.
git -C "$legacy" for-each-ref --format='%(refname) %(objectname)' > "$dir/refs.txt"
git -C "$legacy" stash list --format='%H %gs' > "$dir/stashes.txt" || true
git -C "$legacy" worktree list --porcelain > "$dir/worktrees.txt"
git -C "$legacy" bundle create "$dir/legacy-fork.bundle" --all 2>&1 | tail -1
git -C "$legacy" bundle verify "$dir/legacy-fork.bundle" 2>&1 | tail -1

# 2. Everything a bundle cannot: uncommitted work, per worktree.
{
  echo "# main checkout"
  git -C "$legacy" status --porcelain=v1 --untracked-files=all
  git -C "$legacy" worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
    [[ "$wt" == "$legacy" ]] && continue
    dirty="$(git -C "$wt" status --porcelain=v1 --untracked-files=all 2>/dev/null || true)"
    if [[ -n "$dirty" ]]; then echo "# worktree $wt"; echo "$dirty"; fi
  done
} > "$dir/uncommitted.txt"
uncommitted_lines="$(grep -vc '^#' "$dir/uncommitted.txt" || true)"

# 3. Restore rehearsal: clone the bundle and compare.
scratch="$(mktemp -d)"
git clone --quiet --mirror "$dir/legacy-fork.bundle" "$scratch/restore.git"
restored_refs="$(git -C "$scratch/restore.git" for-each-ref | wc -l | tr -d ' ')"
bundle_refs="$(git -C "$legacy" bundle list-heads "$dir/legacy-fork.bundle" | wc -l | tr -d ' ')"
git -C "$scratch/restore.git" cat-file -e "$head" && restored_head=yes || restored_head=no
rm -rf "$scratch"

shasum -a 256 "$dir/legacy-fork.bundle" > "$dir/legacy-fork.bundle.sha256"
cat > "$dir/MANIFEST.md" <<EOF
# Legacy fork archive $stamp

- Source checkout: $legacy
- HEAD: $head
- Refs in checkout: $(wc -l < "$dir/refs.txt" | tr -d ' ')
- Refs in bundle: $bundle_refs
- Refs after restore rehearsal: $restored_refs
- HEAD present after restore: $restored_head
- Stash entries: $(wc -l < "$dir/stashes.txt" | tr -d ' ') (listed in stashes.txt; stashes are refs and ARE in the bundle)
- Uncommitted or untracked paths not carried by the bundle: $uncommitted_lines (see uncommitted.txt; preserve these by hand before any cleanup)
- Bundle SHA-256: $(cut -d' ' -f1 "$dir/legacy-fork.bundle.sha256")

Restore: \`git clone --mirror legacy-fork.bundle <dir>\`, or fetch it into any repository.
EOF
cat "$dir/MANIFEST.md"
# list-heads can report the same object under several names, so compare against the checkout's
# own ref count: every ref the checkout had must come back, and HEAD must be reachable.
checkout_refs="$(wc -l < "$dir/refs.txt" | tr -d ' ')"
[[ "$restored_head" == yes && "$restored_refs" -ge "$checkout_refs" ]] || { echo "ARCHIVE VERIFICATION FAILED"; exit 1; }
echo "archive: $dir"
