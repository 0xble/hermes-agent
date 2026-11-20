# Credential symlink write guard

Fork patch identity: `credential-symlink-write-guard`.

## Contract

Deny a generic file-tool write when either its normalized lexical path or its
fully resolved realpath matches the existing credential deny-list. Resolve all
symlink components, including the leaf. Host task resolution hands the backend
guard only the realpath, so `write_file` and `patch` (replace and V4A, including
Delete/Move headers) also vet the caller's unresolved, task-anchored spelling
for a credential verdict before resolving it. SSH, container and node paths are
never host-resolved and keep the backend check alone. Keep unrelated symlinks writable and
retain the existing SSH-config approval exception, safe-root target semantics,
NT-namespace pre-resolution check, and Delete/Move entry checks. This remains an
in-process accident-prevention heuristic, not an OS isolation boundary
(`SECURITY.md` §3.2).

## Provenance and adoption

The gap is inherited on fork base `ed599cca9483c7ae13f1120511cec6c147802baf`:
`agent/file_safety.py::_classify_write_denial` discarded the lexical credential
location before applying directory-prefix denies. A leaf symlink from
`.config/gh/hosts.yml` to a synthetic file outside that directory was allowed.
A nested directory symlink could likewise discard the protected prefix.
Through the real tools the gap was wider than the classifier: on base release
`54a91b4f4edf06eeefc87612dc073b91ad06bd3b` and on the first port of this patch,
`patch` rewrote, V4A deleted, and `write_file` created files behind those links,
because the tools resolved the spelling away before the backend guard ran.
The pre-existing `TestProfileHomeProcessHome.test_every_home_is_guarded` assertion
fails with that leaf layout without changing the assertion. Ordinary and
whole-directory symlink layouts are compatibility controls.

Fork and upstream PR searches for symlink credentials found no equivalent fix.
Upstream main's inspected file blob `7778432aa1dd10ae9c375129eabf46a4a27cfc62`
still classified only the realpath for ordinary writes; its entry-only check
protected Delete/Move, not writes. Preserve both coordinates during upstream
sync rather than special-casing GitHub CLI callers or adding credential paths.

Contract tests: `tests/agent/test_file_safety_write_credentials.py`.

All fixtures use synthetic scratch account homes and dummy contents. Run the
canonical test runner for that file and the adjacent file-safety/file-write
suites; qualification remains the exact-SHA gate, not a focused test pass.

## Retirement and rollback

Retire only after the same tests pass against accepted upstream without this
patch, including lexical-outgoing and realpath-incoming symlinks plus benign
and safe-root controls. Roll back the commit carrying this identity; no stored
state or configuration migration is involved.
