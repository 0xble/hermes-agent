# Immutable-release PM extras

Patch identity: `immutable-release-pm-extras`.

The immutable release builder carries enabled leaf extras into the candidate
`.venv`. The first PM generation for that release replaces the payload and
must derive the same declared optional extras from its installed anchors before
writing the new `facts.json`; otherwise a user-enabled feature can disappear
on the first gateway boot.

Memory providers are plugin-owned dependencies, not core optional extras. The
PM dependency workspace therefore adds the configured `memory.provider` to the
selected plugin members (using the same bundled-first lookup as memory
activation), so its manifest `pip_dependencies` enters every new generation.
Do not add a Hindsight anchor to `pm.extras.ANCHORS`: that would make a
plugin-owned dependency sticky after the provider is disabled and would reverse
upstream's plugin-owned-dependency contract.

Regression coverage:
`tests/pm/test_extras.py::test_immutable_release_first_generation_keeps_provider_deps_without_extra`
drives `_target_selection` with no facts or shipped feature list, exercises a
real replacement generation, verifies the provider client is importable, and
checks that disabling the provider removes it from the next generation. It
covers bundled, catalog-installed, and bundled-over-user-copy lookup.
The focused guard is `HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/pm/test_extras.py`.

Retire this record when upstream's immutable-release/PM handoff carries the
configured active plugin's dependency manifest into the first generation with
equivalent regression coverage.
