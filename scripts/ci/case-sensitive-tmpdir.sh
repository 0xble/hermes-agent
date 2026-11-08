#!/usr/bin/env bash
# Provision scratch explicitly, leaving its lifetime with the caller.
set -euo pipefail

if [ "$(uname -s)" != Darwin ] || [ "$#" -gt 1 ]; then
  printf 'Usage (macOS only): %s [new-scratch-directory]\n' "$0" >&2
  exit 2
fi
if [ "$#" -eq 1 ]; then
  mkdir "$1"
  base=$(cd "$1" && pwd -P)
else
  base=$(mktemp -d /private/tmp/hermes-ci-case-sensitive.XXXXXX)
fi
mkdir "$base/mnt"
hdiutil create -size 20g -type SPARSE -fs 'Case-sensitive APFS' \
  -volname hermes-ci-scratch "$base/scratch.sparseimage" >&2
hdiutil attach -nobrowse -mountpoint "$base/mnt" "$base/scratch.sparseimage" >&2
mkdir "$base/mnt/tmp"
printf '%s\n' "$base/mnt/tmp"
printf 'After CI and its children exit, clean up only this image:\n' >&2
printf '  hdiutil detach "%s/mnt"\n  rm "%s/scratch.sparseimage"\n  rmdir "%s/mnt" "%s"\n' \
  "$base" "$base" "$base" "$base" >&2
