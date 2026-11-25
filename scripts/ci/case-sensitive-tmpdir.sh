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

ownership_enabled() {
  diskutil info "$base/mnt" 2>/dev/null |
    grep -Eq '^[[:space:]]*Owners:[[:space:]]+Enabled[[:space:]]*$'
}

if ! hdiutil attach -nobrowse -owners on -mountpoint "$base/mnt" \
  "$base/scratch.sparseimage" >&2; then
  printf 'hdiutil attach -owners on failed; retrying without the ownership flag.\n' >&2
  hdiutil attach -nobrowse -mountpoint "$base/mnt" \
    "$base/scratch.sparseimage" >&2
fi

if ! ownership_enabled; then
  printf 'The scratch volume is not ownership-enabled; trying sudo -n diskutil enableOwnership.\n' >&2
  if ! sudo -n diskutil enableOwnership "$base/mnt" >&2; then
    printf 'Unable to enable ownership on %s; sudo -n is unavailable or failed.\n' \
      "$base/mnt" >&2
    exit 1
  fi
fi
if ! ownership_enabled; then
  printf 'Ownership is still disabled on %s; refusing to use it.\n' "$base/mnt" >&2
  exit 1
fi

mkdir "$base/mnt/tmp"
printf '%s\n' "$base/mnt/tmp"
printf 'After CI and its children exit, clean up only this image:\n' >&2
printf '  hdiutil detach "%s/mnt"\n  rm "%s/scratch.sparseimage"\n  rmdir "%s/mnt" "%s"\n' \
  "$base" "$base" "$base" "$base" >&2
