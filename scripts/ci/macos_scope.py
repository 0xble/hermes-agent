#!/usr/bin/env python3
"""Decide whether a ready PR needs the pre-merge macOS job.

Prints `true` or `false` on stdout and the reason on stderr. The job runs for
fork-sync candidate branches, and for any diff that touches macOS-specific code
or macOS-marked tests, CI configuration, or dependencies. When the diff cannot
be read it prints `true`: unsure means run the job. Routine Linux-only changes
keep the Linux pre-merge gate plus the nightly macOS run.

Usage: macos_scope.py <head-ref> [base-ref]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FORK_SYNC_PREFIX = 'sync/controller-'
# Paths that name macOS or its platform pieces, plus native sources.
MACOS_PATH = re.compile(
    r'(?i)(macos|darwin|osx|launchd|keychain|apple|cocoa|xcode|\bmac[_./-]|[_/-]mac\b)'
    r'|\.(swift|m|mm|plist|entitlements|xcconfig)$')
# Shared setup can change any platform's behavior.
SHARED_SETUP = re.compile(
    r'^(\.github/|bin/ci|scripts/ci/|ci/)'
    r'|(^|/)(conftest\.py|pyproject\.toml|uv\.lock|poetry\.lock|requirements[^/]*\.txt|package(-lock)?\.json'
    r'|pnpm-lock\.yaml|pnpm-workspace\.yaml|\.npmrc|bun\.lockb?|Cargo\.(toml|lock)|go\.(mod|sum))$')
# Python that marks macOS tests or branches on the platform.
MACOS_SOURCE = re.compile(
    r'platforms\([^)]*["\'](macos|posix)["\']'
    r'|sys\.platform\s*[!=]=\s*["\']darwin|sys\.platform\.startswith\(\s*["\']darwin'
    r'|platform\.system\(\)\s*[!=]=\s*["\']Darwin|platform\.mac_ver\(|\bIS_MACOS\b|\bis_macos\b'
    r'|["\'](darwin|Darwin|macos|macOS)["\']')
GIT_TIMEOUT_SECONDS = 120


def git(*args: str) -> str:
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=GIT_TIMEOUT_SECONDS).stdout


def reason_to_run(changed: list[str]) -> str | None:
    for path in changed:
        if SHARED_SETUP.search(path):
            return f'shared setup changed: {path}'
        if MACOS_PATH.search(path):
            return f'macOS-specific path: {path}'
    for path in changed:
        if not path.endswith('.py'):
            continue
        try:
            source = Path(path).read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            # A deleted or unreadable file might have held macOS code: run to be safe.
            return f'changed Python not readable at head: {path}'
        if MACOS_SOURCE.search(source):
            return f'macOS-marked or darwin-specific Python: {path}'
    return None


def decide(head_ref: str, base_ref: str) -> tuple[bool, str]:
    if head_ref.startswith(FORK_SYNC_PREFIX):
        return True, 'fork-sync candidate'
    try:
        base = git('merge-base', base_ref, 'HEAD').strip()
        changed = [p for p in git('diff', '--name-only', '--no-renames', '-z', base, 'HEAD', '--').split('\0') if p]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return True, f'diff unreadable, running to be safe: {exc}'
    reason = reason_to_run(changed)
    return (True, reason) if reason else (False, 'no macOS-specific change')


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    run, reason = decide(argv[0], argv[1] if len(argv) == 2 else 'origin/main')
    print('true' if run else 'false')
    print(f'macos scope: {"run" if run else "skip"} ({reason})', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
