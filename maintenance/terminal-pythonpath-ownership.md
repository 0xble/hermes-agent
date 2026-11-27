# Terminal PYTHONPATH Ownership

Load when changing `tools/environments/local_pythonpath.py`, how terminal children inherit `PYTHONPATH`, or the PM generation layout under `pm.environments.installs_root()`.

## Required behavior and provenance

Fork patch identity: `terminal-pythonpath-ownership`.

The launcher exports the repo root and the selected PM generation's site-packages on `PYTHONPATH` (`pm/environments.py` activation). Terminal children must not inherit those Hermes-owned entries, or a repo venv on a different Python imports Hermes's compiled packages.

On 2026-11-11 a daytime PM sync committed a new generation at 09:48 PT while the gateway still ran on the generation it booted with. Ownership was computed from the current selection only, so the boot generation's site-packages stopped counting as owned and leaked into every terminal child. A Python 3.12 repo venv then failed importing Hermes's Python 3.14 Pillow (`cannot import name '_imaging'`).

Ownership is therefore the whole trusted generation tree, read on every strip:

- Every `installs_root()/*/environments/*/venv` whose resolved path stays inside the resolved installs root and that has `pyvenv.cfg`.
- No one-time cache of the generation set, so a later sync's generation is owned at once.
- Only exact entries are stripped. User venvs, Nix and plugin paths, empty components and another Python's site-packages stay byte-for-byte.

A running gateway's own imported modules stay on its boot generation until the next restart. That is the PM boot-only activation contract, not this unit's concern.

## Verification

`tests/tools/test_local_env_blocklist.py`: the stale-generation regression strips old and new generation site-packages, keeps user and unrelated paths in order, owns a generation created after the first call, and rejects a venv symlink that escapes the installs root.

Contract tests: `tests/tools/test_local_env_blocklist.py`.
