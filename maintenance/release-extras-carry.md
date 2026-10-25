# Release Extras Carry

Patch identity: `release-extras-carry`.

A new immutable release must boot with every optional extra the active release
carries. The active release's extras are inferred from its committed PM
environment (`pm.environments.project_python(previous)`: its PM generation when
one is committed, its `.venv` otherwise) against the candidate's
`pyproject.toml`, by `_committed_extras`: the extras PM recorded in that
release's `facts.json`, plus those inferred from its installed packages (which
covers leaves installed through a composite such as `all`). Two points apply them:

- **Build:** `prepare_venv` builds with the union of the migration-bound source
  interpreter's extras (`release-layout.json` `source_python`) and the active
  release's committed extras. At first migration, when nothing is active yet, the
  source checkout's own committed environment stands in for the active release.
- **Ready candidate:** `stage_release`'s ready path calls `carry_active_extras`,
  which commits the missing extras into the candidate's PM environment with
  `pm.sync_venv(missing, explicit=True, project_root=candidate,
  evict_incompatible_plugins=True)` before activation. Like every other
  update-path sync, it evicts a plugin that keeps the environment from building
  instead of failing the update. An extra counts as missing only when one of its
  applicable requirements is absent from the candidate's boot environment, so a
  platform-gated or already installed extra never re-syncs. A carry records an
  `immutable_extras_carry` step in the update receipt. It runs only when this process's code is the candidate's
  (`pm.paths.repo_root() == candidate`), because PM resolves against the calling
  tree's lockfile.

Why both: `hermes update` from a release re-enters the source checkout
(`hermes_cli/main.py::cmd_update`), and the fetch-only path
(`update_cmd._apply_fetched_immutable_update`) stages the candidate with that
checkout's code, whose HEAD never moves. A fix to `prepare_venv` therefore never
reaches the build on a migrated install. The post-swap child imports the
candidate, reaches `stage_release` through `_activate_immutable_release`, finds
the release ready, and commits the missing extras there. The gateway's first
boot selects that generation.

Incident: on 2026-10-24 every update came up without `slack`. The source
interpreter never had it, because it was added later by lazy installs into each
release's PM generation. The adapter's lazy install committed it after boot,
into a process that could not adopt it, so Slack stayed down until a second
restart. The first fix (build-time only) failed its live canary for the reason
above: release 2bb03a63 was staged by source checkout e8387e2758.

Acknowledgement: a candidate with a committed PM environment boots with
`PYTHONPATH=<release>:<generation site-packages>`, which the launcher's boot
activation (`pm.environments.activate_dependencies`) exports to the gateway it
spawns. The ack used to require an empty `PYTHONPATH`, so the first carried
release (eb37d0af, 2026-10-24) never acknowledged and its promote transaction
stayed pending. `_runs_hermes_main` now qualifies `PYTHONPATH` by launch shape
(`seamless-restart-s2`): the generated `python -I -c` bootstrap ignores it, and
`python -m hermes_cli.main` still requires it empty. `_is_intended_gateway`
skips descendants that exit or are zombies mid-scan, instead of abandoning the
scan on the first `psutil.ZombieProcess`. Never ship the carry without both.

`smoke_plugins` smokes the interpreter the candidate will boot
(`pm.environments.project_python`): the committed generation after a carry, its
`.venv` otherwise.

`_active_locked_extras` returns early when the candidate declares no extras or
has no `pyproject.toml`, so fake releases never spawn their interpreter.

Proof, in `tests/hermes_cli/test_immutable_releases.py`:
`test_prepare_venv_carries_extras_from_previous_release_environment` covers the
build. `test_ready_candidate_from_older_builder_commits_active_extras_before_activation`
replays the canary: the pre-handoff call (source code) must not sync, and the
post-swap call (candidate code) commits `slack`.
`test_sigkill_stage_and_flip_converge_with_complete_current` covers non-git
staging.
`tests/hermes_cli/test_release_ack_environment.py` covers the launch-shape
`PYTHONPATH` matrix and the zombie descendant.

Retire when release staging reads its extras from a PM-owned record that follows
the active install, or when immutable releases are retired. Rollback reverts
`_committed_extras`, `carry_active_extras`, its `stage_release` call, the
`prepare_venv` union and the early return, together with the tests. No state
migration is involved. A PM generation the carry committed stays a normal PM
generation.
