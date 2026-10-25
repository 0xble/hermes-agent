# Release Extras Carry

Patch identity: `release-extras-carry`.

A new immutable release must boot with every optional extra the active release
carries. `prepare_venv` infers the selected extras from two interpreters and
builds the union: the migration-bound source interpreter (`release-layout.json`
`source_python`, the pre-migration checkout's venv) and the active release's
committed PM environment (`pm.environments.project_python(previous)`, which is the
release's PM generation when one is committed and its `.venv` otherwise).

Why both: the source interpreter is frozen at first migration. An extra added
afterwards, by `hermes pm install --extra` or by a gateway lazy install, lands in
the active release's PM generation under `installs/<key>/environments/` and never
reaches that interpreter. Inferring from the source alone rebuilt every release
without it. On 2026-10-24 three consecutive updates came up without `slack`: the
adapter's lazy install committed it into the new release's generation, but the
running process could not adopt it, so Slack stayed down until a second restart,
and the next update dropped it again. The new release's own `.venv` now contains
the carried extras, so the carry also holds for a release whose gateway never
committed a PM generation.

`_active_locked_extras` returns early when the candidate declares no extras or
has no `pyproject.toml`, so fake releases never spawn their interpreter. The
non-git staging path (`source=None`) now reaches inference through the previous
release, which `test_sigkill_stage_and_flip_converge_with_complete_current`
exercises.

Proof: `tests/hermes_cli/test_immutable_releases.py::test_prepare_venv_carries_extras_from_previous_release_environment`
records `slack` only in the previous release's committed generation and fails on
the source-only build (`[['telegram']]`).

Retire when the release build reads its extras from a PM-owned record that
follows the active install, or when immutable releases are retired. Rollback
reverts the `prepare_venv` union and the early return together with the test. No
state migration is involved: releases already built keep their venvs.
