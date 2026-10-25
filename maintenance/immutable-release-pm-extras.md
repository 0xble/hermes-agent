# Immutable-release PM extras

Patch identity: `immutable-release-pm-extras`.

The immutable release builder carries enabled leaf extras with a PM anchor into
the candidate `.venv`. The first PM generation for that release replaces the
payload and must derive the same anchored optional extras from its installed
anchors before writing the new `facts.json`; otherwise a user-enabled feature
with an anchor can disappear on the first gateway boot.

Extras without a PM anchor are not carried by anchor discovery: this currently
includes `hindsight-embedded` (whose modules are `hindsight` and
`hindsight_embed`, not `hindsight_embedded`), plus `cron`, `mcp`, `pty`, `sms`,
`uvloop`, and `nemo-relay`. `hindsight-embedded` needs conditional selection
when `memory.provider == hindsight` and its configured mode is `local_embedded`;
it is a known gap rather than a sticky global anchor. Follow up by teaching the
first-generation handoff to carry that provider mode conditionally without
making the extra survive provider disablement.

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
Bundled providers ship with the release and are not eligible for automatic
plugin eviction. When the selection fails to build, update eviction seats them
before third-party plugins, so a conflicting third-party plugin is disabled with
the usual notice and the update completes
(`tests/pm/test_plugin_survival_contract.py::test_update_sync_evicts_third_party_plugin_conflicting_with_bundled_provider`).
Only a bundled provider that cannot fit the release on its own (a static
misfit such as requires-python) fails the update loudly with the provider config
unchanged (`::test_update_sync_cannot_evict_bundled_memory_provider`).
`_is_bundled_provider` accepts both the bare key and the `memory/<name>` key
(`::test_bundled_provider_recognised_by_bare_and_memory_prefixed_key`).

The Hindsight manifest keeps the old `hindsight` extra's ceiling
(`hindsight-client>=0.6.1,<1`), so the plugin-owned dependency contract is no
looser than the removed extra's.

Known and accepted: a bundled provider's member directory lives inside the
release tree, so the members stamp changes with each immutable release and the
first boot of a new release rebuilds the generation once. That is intended: the
provider's manifest is release-owned and must be re-resolved with each release.

Upstream-status: No equivalent immutable-release PM handoff or bundled-provider eviction guard was found in the upstream baseline; this remains fork-local until an equivalent released implementation exists upstream.

Rollback: Revert the commits carrying `Fork-Patch: immutable-release-pm-extras`; the provider workspace carry-forward and bundled-provider eviction guard should be reverted together.

Retire this record when upstream's immutable-release/PM handoff carries the
configured active plugin's dependency manifest into the first generation with
equivalent regression coverage, and the conditional embedded-provider gap is
closed or intentionally replaced.
